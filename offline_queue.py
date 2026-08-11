"""Durable write-behind queue shared by memnos's THIN CLIENT-SIDE adapters (the Claude
Code hooks in `memnos_cli.py`, the MCP stdio adapter in `memnos_mcp.py`, and any future
thin adapter). PACKAGED as a top-level module (not under `core/`) so importing it never
drags in the server's heavy deps (psycopg/openai/fastembed) — stdlib-only, same
constraint as `nsresolve.py`.

Issue #37 Layer 3 ("durable write-behind to the same store — never diverge"): on a
TRANSIENT failure (server unreachable, or a 5xx from an embed/adapter-time error) a
client-side write is enqueued HERE instead of being lost or improvised into some other
store. It replays into the SAME memnos store once the server answers again — nothing to
reconcile, because there was only ever one store. A PERMANENT failure (401/403/400 — an
auth or validation problem that a retry can never fix) must never be queued: silently
retrying it forever would mask a real problem and eventually paper over data loss with
the illusion of "it'll sync eventually".

Every function takes `config_dir` explicitly (normally `~/.memnos`, or a temp dir in
tests) rather than caching it at import time, so callers can point multiple adapters at
the SAME queue directory (this module doesn't care which adapter produced an item) and
tests never depend on import/reload order.

Layout under config_dir:
    offline_queue/<epoch_ms>_<speaker>_<rand8>.json   — one pending write per file
    recall_snapshot/<safe-namespace>.json             — last-known-good /recall context,
                                                         served (clearly labeled stale)
                                                         when a live recall fails
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid


def queue_dir(config_dir: str) -> str:
    return os.path.join(config_dir, "offline_queue")


def snapshot_dir(config_dir: str) -> str:
    return os.path.join(config_dir, "recall_snapshot")


# ---- failure classification --------------------------------------------------------

_TRANSIENT_EXC_NAMES = {
    # httpx (used by memnos_mcp.py) — TransportError family: the request never reached,
    # or a response was never received from, the server.
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "NetworkError", "TimeoutException", "TransportError",
    "ProtocolError",
    # stdlib urllib/socket (used by memnos_cli.py's hooks)
    "URLError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "OSError", "TimeoutError", "socket.timeout",
}


def is_transient(exc: BaseException) -> bool:
    """True if `exc` represents a failure that is safe to retry — i.e. nothing was
    durably committed server-side, so queuing + replaying later can only ever add the
    write once. False for a PERMANENT failure (bad token, forbidden namespace, bad
    request) that a retry can never turn into success and that must surface to the
    caller immediately instead of queuing silently forever.

    Duck-typed (no hard dependency on httpx) so both the httpx-based MCP adapter and the
    urllib-based CLI hooks can share one classifier:
      - an HTTP status is present (httpx.HTTPStatusError.response.status_code, or
        urllib.error.HTTPError.code) -> 5xx is transient (the server itself failed —
        e.g. an embed-time error inside /remember, per issue #37's "embed-time and
        adapter-time failures" — and P1a's embed call runs BEFORE the turn is stored,
        so a 500 there means nothing landed); 4xx is permanent.
      - no HTTP status at all -> the request never got a response: connection
        refused/reset, DNS/TLS failure, or connect/read timeout. A READ timeout is a
        judgment call: /remember's slow step (EMBED()) happens before the DB write, so a
        timeout most likely means the write never landed either — we queue it and
        accept the small risk of an eventual duplicate turn, since losing the memory
        outright is worse.
      - memnos_mcp.py's own `_post()` re-wraps a connection-down httpx.ConnectError (and
        a 503) into a plain `RuntimeError` with `from None` (a deliberately friendlier
        message for the model) — that suppresses the original exception's __cause__, so
        by the time it reaches here it is indistinguishable from any other RuntimeError
        except by message. Matched by the two exact substrings `_post()` itself uses.
    """
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if "server is not running" in msg or "database unreachable" in msg:
            return True
    status = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)          # urllib.error.HTTPError
    if status is not None:
        try:
            return int(status) >= 500
        except (TypeError, ValueError):
            return False
    return any(c.__name__ in _TRANSIENT_EXC_NAMES for c in type(exc).__mro__)


# ---- write-behind queue: enqueue / drain ---------------------------------------------

def enqueue(config_dir: str, namespace: str, text: str, speaker: str, memory_type: str = "",
            token: str = "") -> str:
    """Durably park one turn for replay into the SAME memnos store, so the caller can
    still return success to whatever is upstream of it (a hook, an MCP tool call) —
    the write is never lost and never diverges into a separate store. Returns the queued
    file's path.

    `token`: the caller's OWN bearer token, live at the moment this item is queued —
    captured here and replayed with THIS item at drain time (see drain()'s `fallback_token`
    param), rather than whatever shared/global token happens to be configured on the
    process that eventually drains the queue. This is what makes queuing safe under the
    streamable-HTTP MCP mount (memnos_server.py): many different callers/tenants share
    ONE mounted memnos_mcp.py process with no per-process token of its own (issue #45) —
    each queued item carries the credential it actually needs to replay successfully,
    which also means a multi-tenant queue drains each item as the tenant that queued it,
    not as whichever tenant's token happened to be live at drain time. Empty string
    (the default) preserves the pre-#45 behavior of relying entirely on the drainer's
    own fallback token — still correct for the single-token-per-process stdio adapter.

    The queue file may now carry a bearer credential, so its permissions are tightened
    to owner-only, same as memnos_cli.py's own config file (best-effort — a chmod
    failure must never block the write itself)."""
    d = queue_dir(config_dir)
    os.makedirs(d, exist_ok=True)
    now = time.time()
    item = {"namespace": namespace, "text": text, "speaker": speaker, "async": True,
            "queued_at": now}
    if memory_type:
        item["type"] = memory_type
    if token:
        item["token"] = token
    fname = f"{int(now * 1000)}_{speaker}_{uuid.uuid4().hex[:8]}.json"
    path = os.path.join(d, fname)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(item, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)                             # atomic: never a partially-written entry
    return path


def _post_remember(url: str, token: str, item: dict, timeout: float = 8) -> None:
    """POST one queued item to the server's /remember. Deliberately whitelists exactly
    the fields below.

    `item["queued_at"]` (captured by enqueue(), also used for FIFO ordering via the
    filename's epoch-ms prefix) IS forwarded — but ONLY as `queued_at`, a hint the server
    uses solely to anchor this fact's EVENT-time (`valid_from`) resolution, so a long
    outage doesn't shift a relative date like "yesterday" to mean the day before replay
    instead of the day before it was actually said (issue #42). It is NEVER forwarded as,
    and the server NEVER treats it as, an observed_at/known_at override: that
    OBSERVATION-axis timestamp — the one bi-temporal supersession is actually keyed on —
    is always the server's own clock at the moment it receives and commits this replayed
    write. Conflating the two would hand a replaying client a timestamp-backdating
    primitive able to force a stale write to win a supersession race against a fact
    someone else wrote while the queue was down. Do not widen this to also send/accept
    an observed_at/known_at field from the client — that reopens exactly the gap issue
    #42 closed. See memnos_server.py's `_remember_phased` / `_replay_valid_anchor` for
    the receiving side of this contract."""
    body = {"namespace": item["namespace"], "text": item["text"],
            "speaker": item.get("speaker"), "async": True}
    if item.get("type"):
        body["type"] = item["type"]
    if item.get("queued_at") is not None:
        body["queued_at"] = item["queued_at"]
    hdr = {"Content-Type": "application/json",
           **({"Authorization": "Bearer " + token} if token else {})}
    req = urllib.request.Request(f"{url.rstrip('/')}/remember", method="POST",
                                  data=json.dumps(body).encode(), headers=hdr)
    urllib.request.urlopen(req, timeout=timeout).read()


_CLAIM_RE = re.compile(r"\.claiming-\d+$")

# Conservative multiple of _post_remember's default 8s timeout: a drainer legitimately
# mid-POST resolves in well under this, so anything older is a claim orphaned by a
# process that died between the claim-rename and the POST resolving.
STALE_CLAIM_AGE = 60.0


def _reclaim_stale_claims(d: str, max_age: float = STALE_CLAIM_AGE) -> None:
    """Recover items orphaned by a drainer that was killed between claiming an item
    (the `os.rename` to `<name>.json.claiming-<pid>`) and that item's POST resolving —
    the exact crash window issue #37 exists to survive. Without this, drain()'s scan
    (which only looks for `*.json`) never finds a `.claiming-*` file again: silent,
    permanent data loss with no error.

    Reclaimed by the CLAIM FILE's mtime age, deliberately NOT by checking whether the
    claiming pid is still alive — a live process legitimately mid-POST must never have
    its claim stolen out from under it (that would double-send the same write). A dead
    pid can also be legitimately reused by an unrelated process on the same machine, so
    pid-liveness isn't even a safe signal.

    `os.rename` is a metadata-only operation and never touches mtime, so the claim call
    below explicitly `os.utime`s each claim at claim time — otherwise a claim's mtime
    would still reflect the original item's ENQUEUE time (possibly hours old, from a
    long outage), and this sweep would steal a perfectly live in-flight claim on sight.
    """
    try:
        entries = os.listdir(d)
    except OSError:
        return
    now = time.time()
    for fname in entries:
        m = _CLAIM_RE.search(fname)
        if not m:
            continue
        path = os.path.join(d, fname)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue                                  # already resolved/removed by its owner
        if age < max_age:
            continue                                  # plausibly still a live in-flight claim
        orig = os.path.join(d, fname[:m.start()])
        try:
            os.rename(path, orig)
        except OSError:
            pass                                       # lost the race with its owner finishing


def drain(config_dir: str, url: str, fallback_token: str, timeout: float = 8, max_items=None) -> tuple[int, int]:
    """Replay queued writes into the SAME store, oldest first (filenames are epoch-ms
    prefixed). Safe for CONCURRENT drainers (a hook's SessionStart drain and an MCP
    adapter's opportunistic drain can legitimately race): each item is atomically
    claimed via `os.rename` to a per-process `.claiming-<pid>` name before it is POSTed,
    so exactly one drainer ever sends a given item — the loser's rename simply fails and
    it moves on.

    Each item is drained with ITS OWN token (captured at enqueue() time, see there) when
    it has one, never a single token shared across the whole queue — issue #45: a queue
    drained under the streamable-HTTP MCP mount can hold items from many different
    tenants/callers, and draining tenant B's item with tenant A's (or nobody's) token
    either 401s a write that would have succeeded, or worse, replays it under the wrong
    principal. `fallback_token` is used only for items that predate per-item token
    capture (pre-#45 queue files already on disk) or that were queued by a caller that
    never had one to give — the single-token-per-process stdio adapter's normal case,
    where the item's own captured token and the drainer's fallback are the same value
    anyway.

    Before scanning, sweeps for and reclaims STALE `.claiming-*` files (see
    `_reclaim_stale_claims`) left behind by a drainer that was killed mid-claim, so a
    crash-during-drain never strands an item outside the `*.json` scan below forever.

    Returns (drained, rejected).
      - On a TRANSIENT failure (server still down/flaky): the claimed item is renamed
        back to its original name and the scan STOPS — chronological order is preserved
        and the whole remaining queue is retried as a unit next time, rather than
        reordering writes.
      - On a PERMANENT failure (401/403/400 — will never succeed): only THAT item is set
        aside with a `.rejected` suffix and the scan CONTINUES — one bad item (e.g. a
        revoked token) must never head-of-line-block every write behind it.
      - A corrupt/unreadable queue file is dropped (removed) rather than blocking.
    """
    d = queue_dir(config_dir)
    if not os.path.isdir(d):
        return 0, 0
    _reclaim_stale_claims(d)
    qfiles = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    drained = rejected = 0
    for fname in qfiles:
        if max_items is not None and drained >= max_items:
            break
        src = os.path.join(d, fname)
        claimed = src + f".claiming-{os.getpid()}"
        try:
            os.rename(src, claimed)
            os.utime(claimed, None)                   # stamp CLAIM time, not enqueue time
        except OSError:
            continue                                  # another drainer already has it, or it's gone
        try:
            item = json.load(open(claimed))
        except Exception:
            try:
                os.remove(claimed)
            except OSError:
                pass
            continue
        item_token = item.get("token") or fallback_token
        try:
            _post_remember(url, item_token, item, timeout=timeout)
        except Exception as e:
            if is_transient(e):
                try:
                    os.rename(claimed, src)           # release — retry the whole queue later
                except OSError:
                    pass                               # a concurrent reclaim already took it
                break
            try:
                os.replace(claimed, src + ".rejected")  # permanent — set aside, keep draining
            except OSError:
                pass                                   # a concurrent reclaim already took it —
            else:                                       # don't double-count what we didn't do
                rejected += 1
            continue
        try:
            os.remove(claimed)
        except OSError:
            pass
        drained += 1
    return drained, rejected


# ---- recall snapshot: last-known-good context for outage-window recall --------------

def _safe_ns_key(namespace: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", namespace or "") or "_"


def save_snapshot(config_dir: str, namespace: str, context: str, mem_count=None) -> None:
    """Cache the last SUCCESSFUL /recall context for `namespace`. Called after every
    live recall so an outage can serve this instead of silently returning nothing (or,
    worse, improvised local data) — the snapshot always came from THIS store, just not
    the current instant of it."""
    if not context:
        return
    d = snapshot_dir(config_dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _safe_ns_key(namespace) + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"namespace": namespace, "context": context, "mem_count": mem_count,
                   "saved_at": time.time()}, fh)
    os.replace(tmp, path)


def load_snapshot(config_dir: str, namespace: str):
    """Return the cached {"namespace", "context", "mem_count", "saved_at"} dict for
    `namespace`, or None if nothing was ever successfully recalled for it."""
    path = os.path.join(snapshot_dir(config_dir), _safe_ns_key(namespace) + ".json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def format_snapshot_age(snapshot: dict) -> str:
    """Human-readable UTC timestamp for the STALE label, e.g. '2026-08-06 14:03 UTC'."""
    import datetime
    ts = (snapshot or {}).get("saved_at")
    if not ts:
        return "an unknown time"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
