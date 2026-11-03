"""Offline staged installation/rollback/uninstall; never use real user locations."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux user installer")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_linux", ROOT/"scripts/install_linux.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    wheelhouse = os.environ.get("TOKENMETER_WHEELHOUSE")
    if not wheelhouse:
        pytest.skip("Offline installer tests require TOKENMETER_WHEELHOUSE")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path/"program with spaces", tmp_path/"applications", tmp_path/"autostart", Path(wheelhouse)


def install(staged, **options):
    prefix, desktop, auto, wheels = staged
    return installer.install(prefix, desktop, auto, wheelhouse=wheels, **options)


def test_offline_install_launcher_update_uninstall_preserves_data(staged, tmp_path):
    prefix, desktop, auto, _ = staged
    install(staged)
    assert not (auto/"tokenmeter.desktop").exists()
    launcher = desktop/"tokenmeter.desktop"
    subprocess.run(["desktop-file-validate", str(launcher)], check=True)
    command = shlex.split(next(line[5:] for line in launcher.read_text().splitlines() if line.startswith("Exec=")))
    data = tmp_path/"user-data"
    data.mkdir(); (data/"keep.txt").write_text("unique user data")
    env = dict(os.environ, TOKENMETER_HOME=str(data), XDG_STATE_HOME=str(tmp_path/"logs"), PYTHONDONTWRITEBYTECODE="1")
    help_run = subprocess.run(command+["--help"], env=env, capture_output=True, text=True, timeout=15)
    assert help_run.returncode == 0, help_run.stderr
    assert "--no-proxy" in help_run.stdout
    # Run the installed code with the test's bounded GTK fixture (not the worktree package).
    env.update(PYTHONPATH=str(ROOT), TMPDIR=str(tmp_path), GSETTINGS_BACKEND="memory",
               GIO_USE_VFS="local", NO_AT_BRIDGE="1", TOKENMETER_HOME=str(tmp_path/"synthetic"))
    if os.environ.get("DISPLAY"):
        output=tmp_path/"installed-ui.json"
        result=subprocess.run([str(prefix/"venv/bin/python"),"-B","-m","tests.linux_smoke_app",str(output),"quit-menu"],
                              cwd=prefix/"app", env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(output.read_text())["closed"]
    install(staged, autostart=True)
    assert (auto/"tokenmeter.desktop").exists()
    assert prefix.with_name(prefix.name+".previous").exists()
    installer.uninstall(prefix)
    assert not prefix.exists() and not prefix.with_name(prefix.name+".previous").exists()
    assert not launcher.exists() and not (auto/"tokenmeter.desktop").exists()
    assert (data/"keep.txt").read_text() == "unique user data"


def test_failed_dependency_update_rolls_back_old_installation(staged, monkeypatch):
    prefix, desktop, _, _ = staged
    install(staged)
    before=installer.inventory(prefix)
    old_launcher=(desktop/"tokenmeter.desktop").read_bytes()
    real=installer.subprocess.run
    def fail(command, **kwargs):
        if 'install' in command and 'pip' in command:
            raise subprocess.CalledProcessError(1, command)
        return real(command, **kwargs)
    monkeypatch.setattr(installer.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        install(staged)
    assert installer.inventory(prefix) == before
    assert (desktop/"tokenmeter.desktop").read_bytes() == old_launcher
    installer.uninstall(prefix)


def test_uninstall_refuses_unique_extra_files(staged):
    prefix, _, _, _ = staged
    install(staged)
    unique=prefix/"unique-work.txt"
    unique.write_text("do not delete")
    with pytest.raises(ValueError, match="modified or extra"):
        installer.uninstall(prefix)
    assert unique.read_text() == "do not delete"
    unique.unlink()  # Known synthetic file owned by this test.
    installer.uninstall(prefix)


def test_distribution_is_deterministic_and_contains_no_private_data(tmp_path):
    paths=[tmp_path/"one.tar.gz",tmp_path/"two.tar.gz"]
    for path in paths:
        subprocess.run([sys.executable,'-B',str(ROOT/'scripts/build_linux_archive.py'),'--output',str(path)], check=True)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    with tarfile.open(paths[0]) as archive:
        names=[member.name for member in archive]
        assert any(name.endswith('/scripts/install_linux.py') for name in names)
        assert not any(any(part in name for part in ('HANDOFF','roadmap/','.git/','.venv','auth.json','usage.db')) for name in names)
        assert not any(name.endswith('.jsonl') or '/tests/test_' in name for name in names)


def test_distribution_excludes_unreviewed_local_files(tmp_path):
    source = tmp_path / "source"
    for name in installer.source_files() + ["scripts/build_linux_archive.py"]:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    sentinel = b"SYNTHETIC_PRIVATE_PUBLICATION_SENTINEL"
    for name in (".env", "auth.json", "developer-notes.md",
                 "tokenmeter/private_notes.py", "tokenmeter/credentials.py"):
        (source / name).write_bytes(sentinel)
    output = tmp_path / "distribution.tar.gz"
    subprocess.run([sys.executable, "-B", str(source / "scripts/build_linux_archive.py"),
                    "--output", str(output)], check=True)
    with tarfile.open(output) as archive:
        assert all(sentinel not in archive.extractfile(member).read()
                   for member in archive.getmembers() if member.isfile())
        assert not any("private_notes" in name or "credentials.py" in name
                       for name in archive.getnames())
