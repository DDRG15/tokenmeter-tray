"""Expose distribution GI in an explicit venv without inheriting other packages.

Run with the distribution Python, e.g. /usr/bin/python3. No GTK/display init,
package installation or system changes; only one symlink inside the given venv.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, required=True)
    args = parser.parse_args()
    venv = args.venv.expanduser().resolve()
    if not (venv / "pyvenv.cfg").is_file():
        parser.error("--venv must name an existing virtual environment")
    try:
        import gi
    except ImportError:
        parser.error("Run this helper with distribution Python that already provides GI")
    # Importing gi checks its compiled binding, but does not initialize GTK.
    source = Path(gi.__file__).resolve().parent
    code = "import json,sys,sysconfig; print(json.dumps([list(sys.version_info[:2]),sysconfig.get_path('purelib')]))"
    query = subprocess.run([str(venv / "bin/python"), "-B", "-c", code],
                           check=True, capture_output=True, text=True, timeout=10)
    version, site = json.loads(query.stdout)
    if version != list(sys.version_info[:2]):
        parser.error("GI and the target venv must use the same Python major/minor version")
    site = Path(site).resolve()
    if not site.is_relative_to(venv):
        parser.error("Refusing to write outside the target virtual environment")
    target = site / "gi"
    if target.is_symlink() and target.resolve() == source:
        return 0
    if target.exists() or target.is_symlink():
        parser.error("The target already contains GI; it will not be overwritten")
    target.symlink_to(source, target_is_directory=True)
    print("Exposed distribution GI inside the target venv; no global packages inherited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
