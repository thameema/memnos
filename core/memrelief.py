"""Return freed heap back to the OS after large recall ops (issue #15).

The recall path transiently allocates (candidate strings, fused result rows, the ONNX
forward-pass working set). glibc/macOS keep that freed memory in the process arena rather
than handing it back to the kernel, so phys_footprint (macOS) / RSS (Linux) ratchets up
and plateaus high even when the heap is mostly free. A periodic, explicit release after a
recall flattens the curve — cheap (microseconds when there's nothing to give back), and a
no-op on platforms without the call. Bounded by a minimum interval so a recall BURST
doesn't pay it on every request.

Knobs (defaults = on, conservative):
  MEMNOS_MEMRELIEF=0           disable entirely
  MEMNOS_MEMRELIEF_MIN_INTERVAL_S=N   min seconds between releases (default 2.0)
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import threading
import time

_lock = threading.Lock()
_last = 0.0
_impl = None            # resolved release callable, or False if none available
_resolved = False


def _enabled() -> bool:
    return os.environ.get("MEMNOS_MEMRELIEF", "1").strip().lower() not in ("0", "false", "no", "off")


def _min_interval() -> float:
    try:
        return float(os.environ.get("MEMNOS_MEMRELIEF_MIN_INTERVAL_S", "2.0"))
    except (TypeError, ValueError):
        return 2.0


def _resolve():
    """Pick the per-platform heap-release call once. Returns a 0-arg callable or None."""
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            # macOS: ask the default malloc zone to release free pages back to the kernel.
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib")
            relief = libc.malloc_zone_pressure_relief
            relief.restype = ctypes.c_size_t
            relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            return lambda: relief(None, 0)        # NULL zone = the default zone
        # Linux / glibc: trim the top of the main arena back to the OS.
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        trim = libc.malloc_trim
        trim.restype = ctypes.c_int
        trim.argtypes = [ctypes.c_size_t]
        return lambda: trim(0)
    except (OSError, AttributeError):
        return None                                # musl (no malloc_trim), exotic libc, etc.


def release(force: bool = False) -> bool:
    """Hand freed heap back to the OS. Rate-limited unless `force`. Returns True if the
    release call actually ran. Never raises — memory relief must never break a request."""
    global _last, _impl, _resolved
    if not _enabled():
        return False
    now = time.monotonic()
    with _lock:
        if not _resolved:
            _impl = _resolve()
            _resolved = True
        if _impl is None:
            return False
        if not force and (now - _last) < _min_interval():
            return False
        _last = now
        impl = _impl
    try:
        impl()
        return True
    except Exception:
        return False
