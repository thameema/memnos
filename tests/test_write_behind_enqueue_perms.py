"""issue #48 — offline_queue.enqueue() TOCTOU on the queue file's permissions.

enqueue() writes a queue item that may carry a bearer token (see enqueue()'s
docstring). The old implementation wrote the tmp file with the builtin `open(tmp,
"w")` — which creates it at the umask-derived default (typically 0644, group/other
READABLE) — and only narrowed it to 0600 with a SEPARATE `os.chmod()` call afterward.
Between those two calls the file sat on disk, readable by any other local user/process,
holding a live bearer token. The fix creates the file at 0600 from the very first
syscall via `os.open(path, O_CREAT|O_EXCL, 0o600)`, so there is no window in which it
is ever anything but owner-only.

A single-threaded test can't literally win a race into that window, so this instead
INSTRUMENTS every syscall capable of creating or widening the tmp file's permissions
(builtin `open`, `os.open`, `os.chmod`) and snapshots the file's mode at each one,
BEFORE that call's effect (if any) narrows it. If the file is ever observed with
group/other bits set at any snapshot, the window existed.

Part 1 proves the harness itself is sound by running it against a reconstruction of
the OLD open-then-chmod pattern and confirming it DOES catch the window.
Part 2 runs the same harness against the real `offline_queue.enqueue()` and confirms
the window is gone.

Run: python tests/test_write_behind_enqueue_perms.py
"""
import json
import os
import shutil
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import offline_queue

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class _PermSpy:
    """Monkeypatches builtin open / os.open / os.chmod for the duration of a `with`
    block and records (label, mode-immediately-after-that-call) for every call that
    touches a path ending in the given suffix — i.e. every point at which the file's
    permissions could be observed by another local process."""

    def __init__(self, suffix):
        self.suffix = suffix
        self.snapshots = []
        self._orig_builtin_open = None
        self._orig_os_open = None
        self._orig_chmod = None

    def _matches(self, path):
        return isinstance(path, str) and path.endswith(self.suffix)

    def __enter__(self):
        import builtins
        self._orig_builtin_open = builtins.open
        self._orig_os_open = os.open
        self._orig_chmod = os.chmod

        def spy_builtin_open(path, *a, **kw):
            fh = self._orig_builtin_open(path, *a, **kw)
            if self._matches(path) and os.path.exists(path):
                self.snapshots.append(("builtin open() created", _mode(path)))
            return fh

        def spy_os_open(path, flags, mode=0o777, *a, **kw):
            fd = self._orig_os_open(path, flags, mode, *a, **kw)
            if self._matches(path):
                self.snapshots.append(("os.open() created", stat.S_IMODE(os.fstat(fd).st_mode)))
            return fd

        def spy_chmod(path, mode, *a, **kw):
            if self._matches(path) and os.path.exists(path):
                # mode the file was sitting at the INSTANT BEFORE chmod narrows it —
                # this is exactly the TOCTOU window the old code exposed.
                self.snapshots.append(("state just before os.chmod()", _mode(path)))
            return self._orig_chmod(path, mode, *a, **kw)

        builtins.open = spy_builtin_open
        os.open = spy_os_open
        os.chmod = spy_chmod
        return self

    def __exit__(self, *exc):
        import builtins
        builtins.open = self._orig_builtin_open
        os.open = self._orig_os_open
        os.chmod = self._orig_chmod


def _buggy_enqueue_reconstruction(config_dir, namespace, text, speaker, token=""):
    """Faithful reconstruction of the PRE-FIX enqueue(): open-then-chmod. Exists only
    to prove _PermSpy actually catches the bug it's meant to catch."""
    import time
    import uuid
    d = offline_queue.queue_dir(config_dir)
    os.makedirs(d, exist_ok=True)
    now = time.time()
    item = {"namespace": namespace, "text": text, "speaker": speaker, "async": True,
            "queued_at": now}
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
    os.replace(tmp, path)
    return path


def main():
    old_umask = os.umask(0o022)  # deterministic: a permissive umask so any un-hardened
                                  # creation call is observably group/other readable
    try:
        # ---- Part 1: harness sanity — it MUST catch the old open-then-chmod window ----
        config_dir = tempfile.mkdtemp(prefix="memnos_wb_perm_buggy_")
        try:
            with _PermSpy(".tmp") as spy:
                _buggy_enqueue_reconstruction(config_dir, "test:perm-sanity", "hi", "user",
                                               token="mnk_should_never_leak")
            widened = [(label, m) for label, m in spy.snapshots if m & 0o077]
            check("harness sanity: reconstructed pre-fix open-then-chmod DOES expose a "
                  f"world/group-readable window ({spy.snapshots})", bool(widened))
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

        # ---- Part 2: the real, fixed enqueue() must show NO such window ever ----
        config_dir = tempfile.mkdtemp(prefix="memnos_wb_perm_fixed_")
        try:
            with _PermSpy(".tmp") as spy:
                path = offline_queue.enqueue(config_dir, "test:perm-fixed", "hi", "user",
                                              token="mnk_should_never_leak")
            check("fixed enqueue() touched the tmp file at least once (spy actually fired)",
                  bool(spy.snapshots))
            widened = [(label, m) for label, m in spy.snapshots if m & 0o077]
            check(f"fixed enqueue() NEVER exposes group/other-readable bits at any point "
                  f"({spy.snapshots})", not widened)
            check("fixed enqueue() never calls os.chmod at all (mode is set at creation)",
                  not any(label.startswith("state just before os.chmod") for label, _ in spy.snapshots))
            check("final queued file is 0600 owner-only",
                  os.path.exists(path) and _mode(path) == 0o600)
            with open(path) as fh:
                saved = json.load(fh)
            check("queued item round-trips the token correctly",
                  saved.get("token") == "mnk_should_never_leak")
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)
    finally:
        os.umask(old_umask)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
