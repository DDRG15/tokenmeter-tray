"""The Linux installer copies a fixed list of files. A module the app imports
but the list forgets installs an app that cannot start, and the test that
installs it for real needs a wheel cache that only the CI job has. This check
needs nothing: it follows the imports of the listed modules."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("install_linux_manifest", ROOT / "scripts" / "install_linux.py")
installer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(installer)


def _module_file(parts: list[str]) -> str | None:
    """The repository path of a `tokenmeter` module or package, if it is one."""
    base = ROOT.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return "/".join(parts) + ".py"
    if (base / "__init__.py").is_file():
        return "/".join(parts) + "/__init__.py"
    return None


def imported_project_files(path: str) -> set[str]:
    """Files of this project that the module at `path` imports, wherever in it."""
    package = path.split("/")[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
        targets: list[list[str]] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
            else:
                base = []
            module = base + (node.module.split(".") if node.module else [])
            if module[:1] != ["tokenmeter"]:
                continue
            targets.append(module)
            # `from . import budgets` names a module, `from .x import f` a function.
            targets += [module + [alias.name] for alias in node.names]
        elif isinstance(node, ast.Import):
            targets += [alias.name.split(".") for alias in node.names
                        if alias.name.split(".")[0] == "tokenmeter"]
        for parts in targets:
            file = _module_file(parts)
            if file:
                found.add(file)
    return found


def test_every_module_the_installed_app_imports_is_in_the_installers_list():
    listed = set(installer.source_files())
    missing = {}
    for path in sorted(p for p in listed if p.startswith("tokenmeter/") and p.endswith(".py")):
        absent = imported_project_files(path) - listed
        if absent:
            missing[path] = sorted(absent)
    assert missing == {}, f"imported but not installed: {missing}"


def test_every_listed_file_exists():
    absent = [name for name in installer.source_files() if not (ROOT / name).is_file()]
    assert absent == []
