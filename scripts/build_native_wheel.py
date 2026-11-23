#!/usr/bin/env python3
"""Build a zero-Python-dependency native platform wheel (`memnos-<ver>-py3-none-<plat>.whl`).

Bundles the compiled `memnos` and `memnos-desktop` executables inside
`memnos-<ver>.data/scripts/` so `uv tool install memnos`, `pipx install memnos`,
and `pip install memnos` install the native single-binary engine and desktop
workspace directly onto `PATH` with zero Python runtime dependencies.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import platform
import re
import stat
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_cargo_version() -> str:
    cargo_toml = os.path.join(REPO_ROOT, "Cargo.toml")
    with open(cargo_toml, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError("Could not parse version from Cargo.toml")
    return m.group(1)


def default_platform_tag() -> str:
    sys_name = sys.platform
    mach = platform.machine().lower()
    if sys_name == "darwin":
        if mach in ("arm64", "aarch64"):
            return "macosx_11_0_arm64"
        return "macosx_10_12_x86_64"
    if sys_name == "win32":
        if mach in ("arm64", "aarch64"):
            return "win_arm64"
        return "win_amd64"
    if sys_name.startswith("linux"):
        if mach in ("aarch64", "arm64"):
            return "manylinux_2_35_aarch64"
        return "manylinux_2_35_x86_64"
    raise RuntimeError(f"Unsupported platform for native wheel: {sys_name} / {mach}")


def sha256_b64_nopad(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_zip_info(arcname: str, executable: bool) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(filename=arcname, date_time=(2026, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.create_system = 3  # Unix permissions metadata (honored by pip/uv on POSIX, ignored safely on Windows)
    mode = (stat.S_IFREG | 0o755) if executable else (stat.S_IFREG | 0o644)
    zi.external_attr = mode << 16
    return zi


def build_wheel(bin_dir: str, out_dir: str, platform_tag: str, version: str) -> str:
    is_win = platform_tag.startswith("win_")
    ext = ".exe" if is_win else ""

    bins = [f"memnos{ext}", f"memnos-desktop{ext}"]
    bin_payloads: list[tuple[str, bytes]] = []
    for bname in bins:
        bpath = os.path.join(bin_dir, bname)
        if not os.path.isfile(bpath):
            raise FileNotFoundError(f"Required release binary not found: {bpath}")
        with open(bpath, "rb") as fh:
            bin_payloads.append((bname, fh.read()))

    readme_path = os.path.join(REPO_ROOT, "README.md")
    with open(readme_path, encoding="utf-8") as fh:
        readme_body = fh.read().strip()

    dist_info = f"memnos-{version}.dist-info"
    data_scripts = f"memnos-{version}.data/scripts"

    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: memnos\n"
        f"Version: {version}\n"
        "Summary: Self-hostable, governed, vendor-neutral memory and multi-agent coordination engine\n"
        "Home-page: https://memnos.net\n"
        "Author: Thameem Ansari\n"
        "License: Apache-2.0\n"
        "Project-URL: Homepage, https://memnos.net\n"
        "Project-URL: Repository, https://github.com/thameema/memnos\n"
        "Project-URL: Documentation, https://github.com/thameema/memnos/tree/master/docs\n"
        "Project-URL: Issues, https://github.com/thameema/memnos/issues\n"
        "Keywords: memory,ai-agents,llm,postgresql,pgvector,rag,mcp,vector-search,long-term-memory,governance\n"
        "Classifier: Development Status :: 4 - Beta\n"
        "Classifier: Intended Audience :: Developers\n"
        "Classifier: License :: OSI Approved :: Apache Software License\n"
        "Classifier: Operating System :: MacOS\n"
        "Classifier: Operating System :: Microsoft :: Windows\n"
        "Classifier: Operating System :: POSIX :: Linux\n"
        "Classifier: Topic :: Scientific/Engineering :: Artificial Intelligence\n"
        "Classifier: Topic :: Database\n"
        "Requires-Python: >=3.8\n"
        "Description-Content-Type: text/markdown\n\n"
        f"{readme_body}\n"
    ).encode("utf-8")

    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: memnos-native-wheel\n"
        "Root-Is-Purelib: false\n"
        f"Tag: py3-none-{platform_tag}\n"
    ).encode("utf-8")

    os.makedirs(out_dir, exist_ok=True)
    whl_name = f"memnos-{version}-py3-none-{platform_tag}.whl"
    whl_path = os.path.join(out_dir, whl_name)

    records: list[str] = []
    with zipfile.ZipFile(whl_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for bname, raw in bin_payloads:
            arc = f"{data_scripts}/{bname}"
            zf.writestr(make_zip_info(arc, executable=True), raw)
            records.append(f"{arc},sha256={sha256_b64_nopad(raw)},{len(raw)}")

        meta_arc = f"{dist_info}/METADATA"
        zf.writestr(make_zip_info(meta_arc, executable=False), metadata)
        records.append(f"{meta_arc},sha256={sha256_b64_nopad(metadata)},{len(metadata)}")

        whl_arc = f"{dist_info}/WHEEL"
        zf.writestr(make_zip_info(whl_arc, executable=False), wheel_meta)
        records.append(f"{whl_arc},sha256={sha256_b64_nopad(wheel_meta)},{len(wheel_meta)}")

        rec_arc = f"{dist_info}/RECORD"
        records.append(f"{rec_arc},,")
        rec_bytes = ("\n".join(records) + "\n").encode("utf-8")
        zf.writestr(make_zip_info(rec_arc, executable=False), rec_bytes)

    return whl_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Build zero-dep native platform wheel for memnos")
    ap.add_argument("--bin-dir", default=os.path.join(REPO_ROOT, "target", "release"))
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "dist"))
    ap.add_argument("--platform-tag", default="")
    ap.add_argument("--version", default="")
    args = ap.parse_args()

    ver = args.version.strip() or read_cargo_version()
    plat = args.platform_tag.strip() or default_platform_tag()
    out = build_wheel(args.bin_dir, args.out_dir, plat, ver)
    print(f"[memnos-wheel] built {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
