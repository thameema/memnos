"""engram-export / engram-import — namespace backup and restore."""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote, urlencode

import httpx

_API_URL = os.environ.get("ENGRAM_API_URL", "http://localhost:8766")
_API_KEY = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_API_KEY}"}


def _cmd_export(args: argparse.Namespace) -> int:
    params: dict = {"ns": args.ns, "format": args.format}
    if args.type:
        params["memory_type"] = args.type
    if args.include_superseded:
        params["include_superseded"] = "true"

    url = f"{_API_URL}/api/v1/admin/export?{urlencode(params)}"
    try:
        with httpx.stream("GET", url, headers=_headers(), timeout=120.0) as resp:
            resp.raise_for_status()
            out_path = args.out
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Exported to {out_path}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    try:
        with open(args.file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"ERROR reading {args.file}: {exc}", file=sys.stderr)
        return 1

    params: dict = {}
    if args.ns:
        params["ns"] = args.ns
    url = f"{_API_URL}/api/v1/admin/import"
    if params:
        url = f"{url}?{urlencode(params)}"

    try:
        resp = httpx.post(url, headers=_headers(), json=data, timeout=120.0)
        resp.raise_for_status()
        result = resp.json()
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"namespace : {result.get('namespace')}")
    print(f"imported  : {result.get('imported')}")
    print(f"skipped   : {result.get('skipped')}")
    return 0


def _cmd_namespaces(args: argparse.Namespace) -> int:
    url = f"{_API_URL}/api/v1/admin/namespaces"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if isinstance(data, list):
        for item in data:
            print(item["name"] if isinstance(item, dict) else item)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="engram-export",
        description="Export and import engram namespace memories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # export
    p_export = sub.add_parser("export", help="Export a namespace to file")
    p_export.add_argument("--ns", required=True, help="Namespace to export")
    p_export.add_argument("--out", required=True, help="Output file path")
    p_export.add_argument("--format", default="json", choices=["json", "csv"],
                          help="Output format (default: json)")
    p_export.add_argument("--type", default=None, dest="type",
                          help="Filter to a specific memory_type")
    p_export.add_argument("--include-superseded", action="store_true",
                          dest="include_superseded",
                          help="Include superseded memories")

    # import
    p_import = sub.add_parser("import", help="Import memories from a JSON export file")
    p_import.add_argument("--file", required=True, help="Path to export JSON file")
    p_import.add_argument("--ns", default=None,
                          help="Override target namespace (default: namespace from file)")

    # namespaces
    sub.add_parser("namespaces", help="List all configured namespaces")

    args = parser.parse_args()
    dispatch = {
        "export": _cmd_export,
        "import": _cmd_import,
        "namespaces": _cmd_namespaces,
    }
    sys.exit(dispatch[args.command](args))
