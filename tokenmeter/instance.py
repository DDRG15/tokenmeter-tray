"""System lock per data folder; released even if the process dies."""

from __future__ import annotations

import os
from pathlib import Path


class InstanceBusy(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, home: Path):
        self.home = Path(home)
        self._file = None

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        fh = (self.home / "instance.lock").open("a+b")
        try:
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise InstanceBusy("TokenMeter is already using this folder. Close the other instance before continuing.") from exc
        self._file = fh
        return self

    def __exit__(self, *_):
        if self._file:
            self._file.close()
            self._file = None
