#!/usr/bin/env python3
"""Real runtime smoke test for a packaged embedded-PG archive: extract it fresh,
initdb, start, create the database, enable pgvector, run a distance query, stop.

Usage: python smoke_test_embedded_pg.py <archive.tar.xz> <workdir> <port>
"""
import os
import subprocess
import sys
import tarfile
import time


def run(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[smoke] {args[0]} failed (exit {r.returncode})\nstdout: {r.stdout}\nstderr: {r.stderr}")
    return r


def main():
    archive, workdir, port = sys.argv[1], sys.argv[2], sys.argv[3]
    exe = ".exe" if sys.platform == "win32" else ""

    os.makedirs(workdir, exist_ok=True)
    print(f"[smoke] extracting {archive}", flush=True)
    with tarfile.open(archive, mode="r:xz") as tf:
        tf.extractall(workdir, filter="data") if sys.version_info >= (3, 12) else tf.extractall(workdir)

    pg_dir = os.path.join(workdir, "memnos-pg16")
    data_dir = os.path.join(workdir, "data")
    log_path = os.path.join(workdir, "pg.log")

    print("[smoke] initdb", flush=True)
    run(os.path.join(pg_dir, "bin", f"initdb{exe}"), "-D", data_dir, "-U", "memnos",
        "--auth", "trust", "--no-instructions", "--encoding", "UTF8", "--locale", "C")

    conf = os.path.join(data_dir, "postgresql.conf")
    with open(conf, "a") as fh:
        fh.write(f"\nport = {port}\nlisten_addresses = '127.0.0.1'\n")

    print(f"[smoke] pg_ctl start (port {port})", flush=True)
    # NOT capture_output=True: on Windows, postgres.exe (the grandchild, which stays
    # running) inherits the pipe's write handle, so communicate() never sees EOF and
    # hangs forever. Real output already goes to -l log_path.
    r = subprocess.run([os.path.join(pg_dir, "bin", f"pg_ctl{exe}"), "start",
                        "-D", data_dir, "-l", log_path, "-w"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    if r.returncode != 0:
        if os.path.isfile(log_path):
            print(open(log_path).read())
        sys.exit(f"[smoke] pg_ctl start failed (exit {r.returncode})")

    try:
        import psycopg
        dsn_admin = f"postgresql://memnos@localhost:{port}/postgres"
        dsn = f"postgresql://memnos@localhost:{port}/memnos"

        last = None
        for _ in range(30):
            try:
                psycopg.connect(dsn_admin, connect_timeout=3).close()
                last = None
                break
            except Exception as e:
                last = e
                time.sleep(1)
        if last:
            sys.exit(f"[smoke] couldn't connect: {last}")

        conn = psycopg.connect(dsn_admin, autocommit=True)
        with conn.cursor() as c:
            c.execute("CREATE DATABASE memnos")
        conn.close()

        conn = psycopg.connect(dsn, autocommit=True)
        with conn.cursor() as c:
            c.execute("CREATE EXTENSION vector")
            c.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            print("[smoke] pgvector version:", c.fetchone()[0])
            c.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")
            print("[smoke] distance op result:", c.fetchone()[0])
        conn.close()
        print("[smoke] PASS")
    finally:
        print("[smoke] pg_ctl stop")
        subprocess.run([os.path.join(pg_dir, "bin", f"pg_ctl{exe}"), "stop", "-D", data_dir, "-w"])


if __name__ == "__main__":
    main()
