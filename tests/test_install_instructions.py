"""A stranger must be able to install and run the project from the README alone.

These checks pin the install contract so it cannot drift silently:

* ``requirements.txt`` holds only what the app needs to run, every package
  (transitive ones included) pinned with ``==``. Ranges like ``>=`` let a fresh
  install pull versions the project never tested (verified on 2026-10-18:
  Pillow 13.0.0 instead of 10.4.0, pytest 9.1.1 instead of 8.2.2).
* ``requirements-dev.txt`` starts with ``-r requirements.txt`` so one command
  installs runtime plus test and build tools, all pinned.
* Every third-party import is covered by those files.
* The README states the Python version, a virtual environment and both
  install commands; CI installs the same file the README documents.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_NOT_FOR_RUNTIME = {"pytest", "playwright", "pyinstaller"}


def requirement_lines(name: str) -> list[str]:
    lines = []
    for raw in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def pinned_names(name: str) -> set[str]:
    return {canonical(line.split("==", 1)[0]) for line in requirement_lines(name)
            if not line.startswith("-r")}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def third_party_imports(paths) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    local = {"tokenmeter", "tests", "launcher"}
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top not in stdlib and top not in local:
                    found.add(top)
    return found


def distributions_for(modules: set[str]) -> dict[str, set[str]]:
    mapping = md.packages_distributions()
    return {m: {canonical(d) for d in mapping.get(m, [])} for m in modules}


def test_dev_requirements_include_runtime_first():
    lines = requirement_lines("requirements-dev.txt")
    assert lines[0] == "-r requirements.txt"


def test_every_requirement_is_pinned_exactly():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in requirement_lines(name):
            if line.startswith("-r"):
                continue
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-]+", line), (name, line)


def test_runtime_requirements_carry_no_test_or_build_tools():
    assert not (pinned_names("requirements.txt") & TOOLS_NOT_FOR_RUNTIME)


def test_no_package_is_pinned_twice():
    assert not (pinned_names("requirements.txt") & pinned_names("requirements-dev.txt"))


def test_runtime_imports_are_covered_by_requirements_txt():
    code = list((ROOT / "tokenmeter").rglob("*.py")) + [ROOT / "launcher.py"]
    runtime = pinned_names("requirements.txt")
    for module, dists in distributions_for(third_party_imports(code)).items():
        assert dists, f"{module} is imported but not installed"
        assert dists & runtime, f"{module} ({dists}) missing from requirements.txt"


def test_test_imports_are_covered_by_dev_requirements():
    tests = list((ROOT / "tests").rglob("*.py"))
    available = pinned_names("requirements.txt") | pinned_names("requirements-dev.txt")
    for module, dists in distributions_for(third_party_imports(tests)).items():
        assert dists & available, f"{module} ({dists}) missing from requirements-dev.txt"


def test_readme_documents_a_clean_install():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needed in ("Python 3.12", "python -m venv .venv", ".venv\\Scripts\\Activate.ps1",
                   "pip install -r requirements.txt", "pip install -r requirements-dev.txt"):
        assert needed in readme, needed
    assert "requirements-lock.txt" not in readme


def test_ci_installs_what_the_readme_documents():
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements-dev.txt" in workflow
    assert "python-version: \"3.12\"" in workflow
