"""Install/uninstall TokenMeter for one user; never launch it or install services."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = '.tokenmeter-install.json'


def source_files(root=ROOT):
    """Only reviewed runtime modules and public documentation enter a distribution."""
    return [
        'CONTRIBUTING.md',
        'LICENSE',
        'MCP.md',
        'LINUX.md',
        'LINUX_DEVELOPMENT.md',
        'README.md',
        'launcher.py',
        'requirements-linux.txt',
        'requirements-mcp.txt',
        'scripts/expose_system_gi.py',
        'scripts/install_linux.py',
        'tests/fixtures/README.md',
        'tokenmeter/__init__.py',
        'tokenmeter/__main__.py',
        'tokenmeter/alerts.py',
        'tokenmeter/anchors.py',
        'tokenmeter/app.py',
        'tokenmeter/assets/tokenmeter.ico',
        'tokenmeter/assets/tokenmeter.png',
        'tokenmeter/backups.py',
        'tokenmeter/billing.py',
        'tokenmeter/calibration.py',
        'tokenmeter/collectors/__init__.py',
        'tokenmeter/collectors/base.py',
        'tokenmeter/collectors/codex.py',
        'tokenmeter/collectors/parsers.py',
        'tokenmeter/collectors/proxy.py',
        'tokenmeter/collectors/transcripts.py',
        'tokenmeter/config.py',
        'tokenmeter/dashboard.py',
        'tokenmeter/exports.py',
        'tokenmeter/files.py',
        'tokenmeter/history.py',
        'tokenmeter/instance.py',
        'tokenmeter/models.py',
        'tokenmeter/mcp.py',
        'tokenmeter/operations.py',
        'tokenmeter/pricing.py',
        'tokenmeter/runtime.py',
        'tokenmeter/session.py',
        'tokenmeter/state.py',
        'tokenmeter/store.py',
        'tokenmeter/ui/__init__.py',
        'tokenmeter/ui/history.py',
        'tokenmeter/ui/icon.py',
        'tokenmeter/ui/linux.py',
        'tokenmeter/ui/notifications.py',
        'tokenmeter/ui/panel.py',
        'tokenmeter/ui/tray.py',
        'tokenmeter/ui/web/index.html',
        'tokenmeter/ui/webserver.py',
        'tokenmeter/ui/window.py',
        'tokenmeter/usage_query.py',
        'tokenmeter/weekly.py',
    ]


def inventory(prefix):
    result = {}
    for folder, dirs, files in os.walk(prefix, followlinks=False):
        for name in list(dirs):
            path = Path(folder)/name
            if path.is_symlink():
                result[str(path.relative_to(prefix))] = 'link:'+os.readlink(path)
                dirs.remove(name)
        for name in files:
            path = Path(folder)/name
            if path == prefix/MARKER:
                continue
            result[str(path.relative_to(prefix))] = ('link:'+os.readlink(path) if path.is_symlink()
                else hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def owned(prefix, *, unchanged=False):
    if prefix.is_symlink() or not (prefix/MARKER).is_file():
        raise ValueError('Refusing to alter an unowned installation prefix')
    data = json.loads((prefix/MARKER).read_text())
    if data.get('format') != 'tokenmeter-install-v1':
        raise ValueError('Unknown installation metadata')
    if unchanged and inventory(prefix) != data['files']:
        raise ValueError('Installation has modified or extra files; preserve/review it before uninstalling')
    return data


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.tokenmeter-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def desktop_arg(value):
    # Desktop Entry string escaping precedes the Exec quoting rules.
    value = str(value)
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('Control characters are not valid in launch paths')
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\'+char)
    return '"'+value.replace('\\','\\\\').replace('%','%%')+'"'


def desktop_entry(prefix):
    python = prefix/'venv/bin/python'
    if '=' in str(python):
        raise ValueError('Desktop executable paths cannot contain =')
    command = ' '.join([desktop_arg(python), '-B', desktop_arg(prefix/'app/launcher.py')])
    return ('[Desktop Entry]\nType=Application\nName=TokenMeter\nComment=Local LLM token usage\n'
            f'Exec={command}\nIcon={prefix}/app/tokenmeter/assets/tokenmeter.png\n'
            'Terminal=false\nCategories=Utility;\nStartupNotify=false\n').encode()


def external_files(data):
    return {Path(p): digest for p, digest in data.get('external', {}).items()}


def uninstall(prefix):
    data = owned(prefix, unchanged=True)
    previous = prefix.with_name(prefix.name+'.previous')
    if previous.exists():
        owned(previous, unchanged=True)
    for path, digest in external_files(data).items():
        if path.name != 'tokenmeter.desktop':
            raise ValueError('Unexpected launcher path in installation metadata')
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('A launcher was edited; it will not be removed')
    for path in external_files(data):
        path.unlink(missing_ok=True)
    shutil.rmtree(prefix)
    if previous.exists():
        shutil.rmtree(previous)


def install(prefix, desktop_dir, autostart_dir, *, autostart=None, wheelhouse=None, python='/usr/bin/python3'):
    prefix = prefix.expanduser().absolute()
    if prefix.is_symlink() or ROOT.is_relative_to(prefix) or prefix.is_relative_to(ROOT) or prefix in (Path('/'), Path.home()):
        raise ValueError('Choose a dedicated application prefix outside the source checkout')
    desktop_dir, autostart_dir = desktop_dir.absolute(), autostart_dir.absolute()
    old = owned(prefix) if prefix.exists() else None
    previous = prefix.with_name(prefix.name+'.previous')
    if previous.exists():
        owned(previous, unchanged=True)
    if autostart is None:
        autostart = bool(old and old.get('autostart'))
    body = desktop_entry(prefix)
    launchers = {desktop_dir/'tokenmeter.desktop': body}
    if autostart:
        launchers[autostart_dir/'tokenmeter.desktop'] = body
    old_external = external_files(old or {})
    for path in set(launchers)|set(old_external):
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != old_external.get(path) and path.read_bytes() != body:
                raise ValueError('An existing launcher is not owned by this installation')
    # Check native dependencies before creating program or launcher files.
    subprocess.run([python, '-B', '-c',
        "import gi; gi.require_version('Gtk','3.0'); gi.require_version('AyatanaAppIndicator3','0.1')"], check=True)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    originals = {path: path.read_bytes() if path.exists() else None for path in set(launchers)|set(old_external)}
    moved = False
    created = False
    with tempfile.TemporaryDirectory(prefix='.tokenmeter-stage-', dir=prefix.parent) as staging:
        stage = Path(staging)
        for name in source_files():
            source = ROOT/name
            if source.is_symlink() or not source.is_file():
                raise ValueError('Missing/unsafe distribution file: '+name)
            target = stage/'app'/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        try:
            if old:
                if previous.exists():
                    shutil.rmtree(previous)
                os.replace(prefix, previous)
                moved = True
            prefix.mkdir()
            created = True
            os.replace(stage/'app', prefix/'app')
            subprocess.run([python, '-B', '-m', 'venv', str(prefix/'venv')], check=True)
            subprocess.run([python, '-B', str(prefix/'app/scripts/expose_system_gi.py'), '--venv', str(prefix/'venv')], check=True)
            command = [str(prefix/'venv/bin/python'), '-B', '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check']
            if wheelhouse:
                command.extend(['--no-index', '--find-links', str(wheelhouse)])
            command.extend(['-r', str(prefix/'app/requirements-linux.txt')])
            subprocess.run(command, check=True)
            subprocess.run([str(prefix/'venv/bin/python'), '-B', '-m', 'pip', 'check'], check=True)
            for path, content in launchers.items():
                atomic(path, content)
            for path in set(old_external)-set(launchers):
                path.unlink(missing_ok=True)
            metadata = {'format':'tokenmeter-install-v1', 'autostart':autostart,
                        'external':{str(p):hashlib.sha256(b).hexdigest() for p,b in launchers.items()},
                        'files':inventory(prefix)}
            atomic(prefix/MARKER, json.dumps(metadata, indent=2).encode())
        except BaseException:
            if created and prefix.exists():
                shutil.rmtree(prefix)
            if moved:
                os.replace(previous, prefix)
            for path, content in originals.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, content)
            raise
    return prefix


def xdg(name, fallback):
    value = os.environ.get(name, '')
    return Path(value) if value and Path(value).is_absolute() else Path.home()/fallback


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local/lib/tokenmeter')
    parser.add_argument('--desktop-dir', type=Path, default=xdg('XDG_DATA_HOME','.local/share')/'applications')
    parser.add_argument('--autostart-dir', type=Path, default=xdg('XDG_CONFIG_HOME','.config')/'autostart')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--autostart', action='store_true', default=None)
    group.add_argument('--no-autostart', action='store_false', dest='autostart')
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--python', default='/usr/bin/python3')
    parser.add_argument('--uninstall', action='store_true')
    args=parser.parse_args(argv)
    if args.uninstall:
        uninstall(args.prefix.expanduser().absolute())
    else:
        install(args.prefix, args.desktop_dir, args.autostart_dir,
                autostart=args.autostart, wheelhouse=args.wheelhouse, python=args.python)
    print('TokenMeter uninstalled; usage/configuration preserved.' if args.uninstall else 'TokenMeter installed; application was not started.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
