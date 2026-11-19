#!/usr/bin/env python3
"""Positive and negative regression tests for `scripts/release_hygiene.sh` archive scanning.

Proves that `release_hygiene.sh --dist-only` catches builder path leaks (POSIX `/Users/...`,
Windows `C:\\Users\\...`, `.cargo/registry`, `rust/src/`) and test-only switches inside unpacked
`.tar.gz` and `.whl`/`.zip` archives — not merely passing on clean inputs.
"""
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_SH = os.path.join(REPO_ROOT, "scripts", "release_hygiene.sh")

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" ({detail.strip()})" if detail else ""), file=sys.stderr)


def make_tgz(path: str, member_name: str, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))


def make_whl(path: str, member_name: str, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member_name, payload)


def run_gate_dist(dist_dir: str) -> subprocess.CompletedProcess:
    bash_bin = "bash"
    return subprocess.run(
        [bash_bin, GATE_SH, "--dist-only", dist_dir],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )


def main() -> int:
    print("=== release_hygiene.sh positive + negative archive tests ===")

    # 1. Clean .tar.gz + .whl -> MUST PASS (exit 0)
    with tempfile.TemporaryDirectory(prefix="hygiene_clean_") as d:
        make_tgz(os.path.join(d, "memnos-clean.tar.gz"), "bin/memnos", b"clean-release-payload-v0.1.64\n")
        make_whl(os.path.join(d, "memnos-0.1.64-py3-none-any.whl"), "memnos/info.txt", b"clean-wheel-payload\n")
        r = run_gate_dist(d)
        check("Clean .tar.gz and .whl pass release_hygiene.sh", r.returncode == 0, r.stdout + r.stderr)

    # 2. Leaky POSIX path inside .tar.gz -> MUST FAIL (exit != 0)
    with tempfile.TemporaryDirectory(prefix="hygiene_posix_leak_") as d:
        make_tgz(
            os.path.join(d, "memnos-leaky-posix.tar.gz"),
            "bin/memnos",
            b"panic at /Users/builder/.cargo/registry/src/crate/lib.rs:42\n",
        )
        r = run_gate_dist(d)
        check(
            "Deliberately leaky .tar.gz (POSIX /Users/ + .cargo/registry) FAILS release_hygiene.sh",
            r.returncode != 0 and "FAILED" in (r.stderr + r.stdout),
            r.stdout + r.stderr,
        )

    # 3. Leaky Windows native path inside .whl (.zip) -> MUST FAIL (exit != 0)
    with tempfile.TemporaryDirectory(prefix="hygiene_win_leak_") as d:
        make_whl(
            os.path.join(d, "memnos-0.1.64-py3-none-any.whl"),
            "memnos/native.bin",
            b"panic at C:\\Users\\runneradmin\\.cargo\\registry\\src\\crate\\lib.rs:99\n",
        )
        r = run_gate_dist(d)
        check(
            "Deliberately leaky .whl (Windows C:\\Users\\ + .cargo\\registry) FAILS release_hygiene.sh",
            r.returncode != 0 and "FAILED" in (r.stderr + r.stdout),
            r.stdout + r.stderr,
        )

    # 4. Leaky test-only switch inside .tar.gz -> MUST FAIL (exit != 0)
    with tempfile.TemporaryDirectory(prefix="hygiene_switch_leak_") as d:
        make_tgz(
            os.path.join(d, "memnos-leaky-switch.tar.gz"),
            "bin/memnos",
            b"enabled MEMNOS_FAKE_EXTRACT_MODE in production\n",
        )
        r = run_gate_dist(d)
        check(
            "Deliberately leaky .tar.gz (FAKE_EXTRACT test switch) FAILS release_hygiene.sh",
            r.returncode != 0 and "FAILED" in (r.stderr + r.stdout),
            r.stdout + r.stderr,
        )

    # 5. Empty dist directory with --dist-only -> MUST FAIL (exit != 0)
    with tempfile.TemporaryDirectory(prefix="hygiene_empty_") as d:
        r = run_gate_dist(d)
        check(
            "Empty dist directory with --dist-only FAILS release_hygiene.sh (no silent vacuous pass)",
            r.returncode != 0,
            r.stdout + r.stderr,
        )

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
