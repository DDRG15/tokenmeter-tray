"""The .exe entry point respects the CLI flags."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_launcher():
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exe_entry_point_passes_flags_to_the_cli(tmp_path, monkeypatch):
    """`TokenMeter.exe --no-proxy` used to open the proxy anyway."""
    monkeypatch.setenv("TOKENMETER_HOME", str(tmp_path))
    received = {}

    import tokenmeter.__main__ as cli

    monkeypatch.setattr(cli, "main", lambda argv: received.setdefault("argv", argv) and 0)
    monkeypatch.setattr(sys, "argv", ["TokenMeter.exe", "--no-proxy", "--no-transcripts"])

    root_logger = logging.getLogger()
    before = list(root_logger.handlers)
    try:
        load_launcher().main()
    finally:
        for h in root_logger.handlers[:]:
            if h not in before:
                root_logger.removeHandler(h)
                h.close()

    assert received["argv"] == ["--no-proxy", "--no-transcripts"]
    assert (tmp_path / "tokenmeter.log").exists()
