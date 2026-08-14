"""issue #63 — offline_queue.save_snapshot() creates the recall-snapshot cache with no
permission hardening at all.

save_snapshot() writes the last-known-good `/recall` context — actual recalled memory
content, not just a token — via plain `open(tmp, "w")` -> `os.replace()`, with no
chmod at all. The tmp file sits at the umask-derived default (typically 0644,
group/other readable) for as long as it exists, and since nothing ever narrows it
afterward, so does the published file if the write is ever left mid-flight.

issue #63's own review comment warns against naively porting #62's exact fix
(deterministic tmp path + `os.open(..., O_EXCL, 0o600)`) from enqueue() onto this
function: enqueue()'s tmp filename is fresh every call, so O_EXCL never collides with
itself. save_snapshot()'s tmp path was deterministic PER NAMESPACE — a crash between
create and os.replace() would leave a stray tmp file that then permanently raises
FileExistsError on every future snapshot write for that namespace, since nothing
sweeps or unlinks orphaned tmp files. Worse, save_snapshot() genuinely races: two
sessions on the same namespace (memnos_cli.py's hook is a fresh process per prompt)
can both call it around the same moment, so a deterministic-tmp+O_EXCL port would also
misfire on a live concurrent writer, not just a crash leftover.

The fix: each save_snapshot() attempt gets its OWN unique tmp filename (epoch-ms +
random suffix, matching enqueue()'s pattern) created at 0600 via O_EXCL, plus a
best-effort age-based sweep of orphaned tmp files so a crash leftover doesn't
accumulate forever.

This test proves, in four parts:
  1. Harness sanity — the CURRENT (pre-fix) open()-then-nothing code DOES expose a
     world/group-readable window (the actual bug #63 reports).
  2. Harness sanity — naively porting #62's deterministic-tmp+O_EXCL fix DOES break
     permanently after a simulated crash leftover (the bug the issue's review comment
     warns against — proves the chosen fix shape is actually necessary).
  3. The real, fixed save_snapshot() is 0600 from the very first syscall, never widens.
  4. The real, fixed save_snapshot() survives a simulated crash leftover (stale orphan
     tmp file) for the SAME namespace without breaking, sweeps it up, and leaves a
     FRESH (plausibly in-flight) leftover alone.

Run: python tests/test_write_behind_snapshot_perms.py
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid

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
    block and records (label, mode-immediately-after-that-call) for every call whose
    path CONTAINS the given substring — save_snapshot()'s tmp filename now varies per
    attempt, so this matches on a substring (".tmp") rather than a fixed suffix."""

    def __init__(self, substr):
        self.substr = substr
        self.snapshots = []
        self._orig_builtin_open = None
        self._orig_os_open = None
        self._orig_chmod = None

    def _matches(self, path):
        return isinstance(path, str) and self.substr in path

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


def _pre_fix_save_snapshot(config_dir, namespace, context, mem_count=None):
    """Faithful reconstruction of the CURRENT (pre-#63) save_snapshot(): plain
    open()-then-os.replace(), no chmod at all. Exists only to prove _PermSpy actually
    catches the bug #63 reports."""
    d = offline_queue.snapshot_dir(config_dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, offline_queue._safe_ns_key(namespace) + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"namespace": namespace, "context": context, "mem_count": mem_count,
                   "saved_at": time.time()}, fh)
    os.replace(tmp, path)
    return path


def _naive_ported_save_snapshot(config_dir, namespace, context, mem_count=None):
    """Reconstruction of the NAIVE port of #62's fix onto save_snapshot(): keeps the
    deterministic per-namespace tmp path, just swaps open()->os.open(O_EXCL, 0o600).
    Exists only to prove this specific shape breaks permanently after a crash leftover
    — exactly what issue #63's review comment warns against, and exactly why the real
    fix uses a unique tmp filename per attempt instead."""
    d = offline_queue.snapshot_dir(config_dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, offline_queue._safe_ns_key(namespace) + ".json")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"namespace": namespace, "context": context, "mem_count": mem_count,
                   "saved_at": time.time()}, fh)
    os.replace(tmp, path)
    return path


def main():
    old_umask = os.umask(0o022)  # deterministic: a permissive umask so any un-hardened
                                  # creation call is observably group/other readable
    try:
        # ---- Part 1: harness sanity — the CURRENT pre-fix code exposes a real window --
        config_dir = tempfile.mkdtemp(prefix="memnos_wb_snap_perm_buggy_")
        try:
            with _PermSpy(".tmp") as spy:
                _pre_fix_save_snapshot(config_dir, "test:snap-perm-sanity",
                                        "some recalled memory content")
            widened = [(label, m) for label, m in spy.snapshots if m & 0o077]
            check("harness sanity: reconstructed pre-fix open()-then-replace DOES "
                  f"expose a world/group-readable window ({spy.snapshots})", bool(widened))
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

        # ---- Part 2: harness sanity — naively porting #62's O_EXCL fix breaks on a --
        # ---- simulated crash leftover, proving the review comment's warning is real --
        config_dir = tempfile.mkdtemp(prefix="memnos_wb_snap_naive_port_")
        try:
            ns = "test:snap-naive-port"
            path = _naive_ported_save_snapshot(config_dir, ns, "first successful save")
            # simulate a crash: a process created the deterministic tmp file but died
            # before os.replace() ever ran, leaving it stranded.
            stray_tmp = path + ".tmp"
            with open(stray_tmp, "w") as fh:
                fh.write("{}")
            os.chmod(stray_tmp, 0o600)
            raised = False
            try:
                _naive_ported_save_snapshot(config_dir, ns, "second save after crash")
            except FileExistsError:
                raised = True
            check("harness sanity: naive deterministic-tmp+O_EXCL port DOES permanently "
                  "break future saves after a crash leftover (raises FileExistsError, "
                  "exactly the bug #63's review comment warns against)", raised)
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

        # ---- Part 3: the real, fixed save_snapshot() must show NO perm window ever ----
        config_dir = tempfile.mkdtemp(prefix="memnos_wb_snap_fixed_perm_")
        try:
            with _PermSpy(".tmp") as spy:
                offline_queue.save_snapshot(config_dir, "test:snap-perm-fixed",
                                             "some recalled memory content", mem_count=3)
            # save_snapshot() has no return value; compute the published path the same
            # way the module does, for the final on-disk assertions below.
            path = os.path.join(offline_queue.snapshot_dir(config_dir),
                                 offline_queue._safe_ns_key("test:snap-perm-fixed") + ".json")
            opens = [s for s in spy.snapshots if s[0] == "os.open() created"]
            check("fixed save_snapshot() created its tmp file via os.open() (spy actually "
                  f"fired on the real code path, not a no-op) ({spy.snapshots})", bool(opens))
            widened = [(label, m) for label, m in spy.snapshots if m & 0o077]
            check(f"fixed save_snapshot() NEVER exposes group/other-readable bits at any "
                  f"point ({spy.snapshots})", not widened)
            check("fixed save_snapshot() never calls os.chmod at all (mode is set at "
                  "creation)",
                  not any(label.startswith("state just before os.chmod") for label, _ in spy.snapshots))
            check("final published snapshot file is 0600 owner-only",
                  os.path.exists(path) and _mode(path) == 0o600)
            with open(path) as fh:
                saved = json.load(fh)
            check("saved snapshot round-trips its content correctly",
                  saved.get("context") == "some recalled memory content" and saved.get("mem_count") == 3)
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

        # ---- Part 4: adversarial — a crash leftover for this namespace must NOT -------
        # ---- permanently break future saves, and the sweep must respect an in-flight --
        # ---- (fresh) leftover belonging to a genuine concurrent writer -----------------
        config_dir = tempfile.mkdtemp(prefix="memnos_wb_snap_crash_")
        try:
            d = offline_queue.snapshot_dir(config_dir)
            os.makedirs(d, exist_ok=True)
            ns = "test:snap-crash-recovery"
            safe = offline_queue._safe_ns_key(ns)
            path = os.path.join(d, safe + ".json")

            # first, a normal successful save, so there's a real prior snapshot on disk.
            offline_queue.save_snapshot(config_dir, ns, "context before the crash")
            check("baseline save succeeded before simulating a crash", os.path.exists(path))

            # simulate a crash: an orphaned tmp file in THIS fix's own naming scheme,
            # left behind after create but before os.replace() — backdated well past
            # the staleness threshold so the sweep is guaranteed to consider it stale.
            stale_tmp = os.path.join(d, f"{safe}.json.tmp-{int(time.time() * 1000) - 999999}-{uuid.uuid4().hex[:8]}")
            fd = os.open(stale_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fdopen(fd, "w").write('{"context": "orphaned from a crash"}')
            old_time = time.time() - (offline_queue.STALE_SNAPSHOT_TMP_AGE + 300)
            os.utime(stale_tmp, (old_time, old_time))

            # also drop a FRESH tmp file — simulates a genuine concurrent writer that is
            # legitimately mid-write right now. The sweep must NOT touch this one.
            fresh_tmp = os.path.join(d, f"{safe}.json.tmp-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}")
            fd2 = os.open(fresh_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fdopen(fd2, "w").write('{"context": "a live concurrent writer, still mid-flight"}')

            raised = False
            try:
                offline_queue.save_snapshot(config_dir, ns, "context after the crash")
            except FileExistsError:
                raised = True
            check("save_snapshot() for the SAME namespace after a crash leftover does "
                  "NOT raise (the actual bug #63 exists to prevent)", not raised)
            with open(path) as fh:
                saved = json.load(fh)
            check("the post-crash save actually landed the new content",
                  saved.get("context") == "context after the crash")
            check("the stale (crash-orphaned) tmp file was swept up",
                  not os.path.exists(stale_tmp))
            check("a FRESH (plausibly in-flight) leftover tmp file was left untouched by "
                  "the sweep — a genuine concurrent writer's in-progress file is never "
                  "clobbered", os.path.exists(fresh_tmp))
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)
    finally:
        os.umask(old_umask)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
