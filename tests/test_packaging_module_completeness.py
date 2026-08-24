"""Packaging completeness gate (regression guard for the memnos_gateway omission found
during the batch-7 release readiness check, 2026-08): every top-level module that a
PACKAGED file `import`s (module-level OR inside a function body — the gateway import in
memnos_cli.py's `cmd_gateway()` is exactly this shape) must itself be listed in
pyproject.toml's `[tool.setuptools] py-modules`, or it silently ships a wheel/sdist
missing code a packaged entry point depends on at runtime.

This class of bug is invisible to `tests/test_*.py` run from a source checkout (imports
resolve fine against the working tree) AND to CI's `cli-smoke` job (`pip install .` +
`--help`/`-V`/docs-only — never imports the module that's actually missing). It only
shows up when someone runs the BUILT wheel, which is exactly what a freshly-installed
PyPI package is. `memnos_gateway.py` (issue #37 Layer 2) was added to the repo but never
added to `py-modules`, so `memnos start`/`memnos gateway` (the default startup path per
memnos_cli.py's own docstring) would raise ModuleNotFoundError on a real `pip install
memnos` until this test's fix landed.

No DB, no server, no build step (static AST scan only — much cheaper than an actual
`python -m build` + wheel inspection, and just as effective for this exact bug class).
Run: python tests/test_packaging_module_completeness.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _parse_py_modules(pyproject_text):
    m = re.search(r"py-modules\s*=\s*\[(.*?)\]", pyproject_text, re.S)
    assert m, "could not find [tool.setuptools] py-modules = [...] in pyproject.toml"
    return {tok.strip().strip('"').strip("'") for tok in m.group(1).split(",") if tok.strip()}


def _parse_packages(pyproject_text):
    m = re.search(r"^packages\s*=\s*\[(.*?)\]", pyproject_text, re.S | re.M)
    assert m, "could not find [tool.setuptools] packages = [...] in pyproject.toml"
    return {tok.strip().strip('"').strip("'") for tok in m.group(1).split(",") if tok.strip()}


def _local_top_level_modules():
    """Every *.py file sitting directly in the repo root, by module name."""
    return {f[:-3] for f in os.listdir(ROOT)
            if f.endswith(".py") and os.path.isfile(os.path.join(ROOT, f))}


def _imported_top_level_names(py_file, local_modules):
    """All `import X` / `from X import ...` names in py_file (any nesting depth) that
    refer to one of this repo's own top-level modules — i.e. local, packageable code,
    not stdlib/third-party."""
    tree = ast.parse(open(py_file, encoding="utf-8").read(), filename=py_file)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in local_modules:
                    found.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top = node.module.split(".")[0]
                if top in local_modules:
                    found.add(top)
    return found


def main():
    pyproject_text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    shipped_modules = _parse_py_modules(pyproject_text)   # top-level .py files that ARE packaged
    packages = _parse_packages(pyproject_text)             # packaged directories (core, integrations, ...)
    local_modules = _local_top_level_modules()

    print(f"=== py-modules (shipped top-level files) === {sorted(shipped_modules)}")
    print(f"=== local top-level .py files present in repo === {sorted(local_modules)}")

    # The entry point itself (memnos_cli, target of `memnos = "memnos_cli:main"`) is always
    # shipped by definition; sanity-check that assumption stays true.
    check("entry-point module memnos_cli.py is in py-modules", "memnos_cli" in shipped_modules)

    # Scan every PACKAGED file (the shipped top-level modules + everything under the
    # packaged directories) for imports of local top-level modules that AREN'T shipped.
    scan_files = [os.path.join(ROOT, f"{m}.py") for m in shipped_modules]
    for pkg in packages:
        pkg_dir = os.path.join(ROOT, pkg.replace(".", os.sep))
        if os.path.isdir(pkg_dir):
            for fname in os.listdir(pkg_dir):
                if fname.endswith(".py"):
                    scan_files.append(os.path.join(pkg_dir, fname))

    missing = {}   # unshipped local module -> set of packaged files that import it
    for f in scan_files:
        if not os.path.isfile(f):
            continue
        for name in _imported_top_level_names(f, local_modules):
            if name not in shipped_modules:
                missing.setdefault(name, set()).add(os.path.relpath(f, ROOT))

    print(f"=== packaged files scanned === {len(scan_files)}")
    check(
        "no packaged file imports a top-level module missing from py-modules",
        not missing,
        "; ".join(f"{mod!r} imported by {sorted(files)}" for mod, files in missing.items()),
    )

    # Direct regression pin for the specific bug this test was written for.
    check("memnos_gateway is in py-modules (issue #37 Layer 2 packaging fix)",
          "memnos_gateway" in shipped_modules)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
