#!/usr/bin/env python3
"""Package the memnos embedded-PG Windows release asset.

zonky's embedded-postgres-binaries ship a stripped-down PostgreSQL with no headers,
no pg_config, and no pgvector — same gap as the macOS/Linux assets. This script
takes zonky's windows-amd64 PG16 bundle and merges in a pgvector build that a prior
CI step already compiled + installed (via nmake) against a full local PostgreSQL 16
install, producing the same `memnos-pg16/{bin,lib,share}` layout used by the
macOS/Linux archives at the `embedded-pg-v1` release tag.

Run on a windows-latest GitHub Actions runner, after:
  1. installing a full PostgreSQL 16.x (e.g. `choco install postgresql16`) — this is
     ONLY needed to get pg_config + headers + import libs to compile against; the
     shipped asset uses zonky's binaries, not this install.
  2. building + installing pgvector against it: `nmake /F Makefile.win` +
     `nmake /F Makefile.win install` (installs into that same PG16's lib/share).

Usage:
    python build_embedded_pg_windows.py <pg16_install_dir> <zonky_pg_version> <out.tar.xz>
"""
import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile


def main():
    pg_install_dir, zonky_version, out_path = sys.argv[1:4]

    zonky_url = (
        "https://repo1.maven.org/maven2/io/zonky/test/postgres/"
        f"embedded-postgres-binaries-windows-amd64/{zonky_version}/"
        f"embedded-postgres-binaries-windows-amd64-{zonky_version}.jar"
    )
    print(f"[build] downloading {zonky_url}")
    with urllib.request.urlopen(zonky_url, timeout=300) as resp:
        jar_bytes = resp.read()

    work = "embedded_pg_build"
    if os.path.isdir(work):
        shutil.rmtree(work)
    pg_dir = os.path.join(work, "memnos-pg16")
    os.makedirs(pg_dir)

    with zipfile.ZipFile(io.BytesIO(jar_bytes)) as zf:
        txz_name = next(n for n in zf.namelist() if n.endswith(".txz"))
        txz_bytes = zf.read(txz_name)
    print(f"[build] extracting {txz_name} ({len(txz_bytes) / 1024 / 1024:.1f} MB)")
    with tarfile.open(fileobj=io.BytesIO(txz_bytes), mode="r:xz") as tf:
        tf.extractall(pg_dir, filter="data") if sys.version_info >= (3, 12) else tf.extractall(pg_dir)

    # Merge in the pgvector build this runner already compiled + installed —
    # Windows PG layout is flat (share/extension/, lib/*.dll), unlike the
    # Debian-style share/postgresql/extension/ nesting zonky uses on Linux.
    vector_dll = os.path.join(pg_install_dir, "lib", "vector.dll")
    ext_dir = os.path.join(pg_install_dir, "share", "extension")
    ext_files = [f for f in os.listdir(ext_dir) if f.startswith("vector")] if os.path.isdir(ext_dir) else []
    if not os.path.isfile(vector_dll) or not ext_files:
        sys.exit(f"[build] pgvector build not found under {pg_install_dir} "
                  f"(expected lib/vector.dll + share/extension/vector*) — did nmake install run?")

    shutil.copy2(vector_dll, os.path.join(pg_dir, "lib", "vector.dll"))
    for f in ext_files:
        shutil.copy2(os.path.join(ext_dir, f), os.path.join(pg_dir, "share", "extension", f))
    print(f"[build] merged vector.dll + {len(ext_files)} share/extension file(s)")

    print(f"[build] packaging {out_path}")
    with tarfile.open(out_path, mode="w:xz") as tf:
        tf.add(pg_dir, arcname="memnos-pg16")

    print(f"[build] wrote {out_path} ({os.path.getsize(out_path) / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
