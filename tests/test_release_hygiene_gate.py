#!/usr/bin/env python3
"""Positive and negative regression tests for `scripts/release_hygiene.sh` archive scanning.

Proves that `release_hygiene.sh --dist-only` catches builder path leaks (POSIX `/Users/...`,
Windows `C:\\Users\\...`, `.cargo/registry`, `rust/src/`) and test-only switches inside unpacked
`.tar.gz` and `.whl`/`.zip` archives — not merely passing on clean inputs.
"""
import io
import os
import shutil
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


def find_bash() -> str:
    env_bash = os.environ.get("BASH_BIN", "").strip()
    if env_bash and os.path.isfile(env_bash):
        return env_bash
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf64 = os.environ.get("ProgramW6432", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for root in (pf, pf64, pf86):
            for rel in (("Git", "bin", "bash.exe"), ("Git", "usr", "bin", "bash.exe")):
                cand = os.path.join(root, *rel)
                if os.path.isfile(cand):
                    return cand
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            cand = os.path.join(d, "bash.exe")
            norm = os.path.normcase(os.path.abspath(cand))
            if (
                os.path.isfile(cand)
                and r"\system32\bash.exe" not in norm
                and r"\sysnative\bash.exe" not in norm
                and r"\windowsapps\bash.exe" not in norm
            ):
                return cand
    return shutil.which("bash") or "bash"


BASH_EXE = find_bash()


def run_gate_dist(dist_dir: str) -> subprocess.CompletedProcess:
    gate_arg = GATE_SH.replace("\\", "/") if os.name == "nt" else GATE_SH
    dist_arg = dist_dir.replace("\\", "/") if os.name == "nt" else dist_dir
    return subprocess.run(
        [BASH_EXE, gate_arg, "--dist-only", dist_arg],
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

    # 6. Corrupt / unreadable .tar.gz -> MUST FAIL (no silent pipe error swallow)
    with tempfile.TemporaryDirectory(prefix="hygiene_corrupt_") as d:
        with open(os.path.join(d, "memnos-corrupt.tar.gz"), "wb") as fh:
            fh.write(b"not-a-valid-gzip-stream")
        r = run_gate_dist(d)
        check(
            "Corrupt .tar.gz FAILS release_hygiene.sh (no silent unpack error swallow)",
            r.returncode != 0 and "FAILED" in (r.stderr + r.stdout),
            r.stdout + r.stderr,
        )

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
