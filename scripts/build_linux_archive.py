"""Build a deterministic source distribution using an explicit runtime allowlist."""
from __future__ import annotations
import argparse
import gzip
import io
from pathlib import Path
import tarfile
from install_linux import ROOT, source_files


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('wb') as stream, gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as archive:
            for name in source_files()+['scripts/build_linux_archive.py']:
                path=ROOT/name
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Unsafe distribution member: '+name)
                body=path.read_bytes()
                member=tarfile.TarInfo('TokenMeter-linux/'+name)
                member.size=len(body); member.mode=0o644; member.mtime=0
                archive.addfile(member, io.BytesIO(body))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
