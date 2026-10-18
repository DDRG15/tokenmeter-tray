"""Entry point of the packaged executable.

PyInstaller needs a real script as its root; `python -m tokenmeter` does not
work for it. This file exists only for that — the code lives in the package.

Without a console there is nowhere to see a startup failure, so this one does
write a log to disk and shows an error box: otherwise an early error would close
the app without a trace and without anything to show the user.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import sys
import traceback


def main() -> int:
    from tokenmeter.config import data_dir

    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(folder / "tokenmeter.log", maxBytes=2*1024*1024,
                                      backupCount=3, encoding="utf-8")],
    )

    # The same CLI as `python -m tokenmeter`. `App` used to be built by hand and
    # the .exe ignored every flag: `TokenMeter.exe --no-proxy` started the proxy
    # anyway. The CLI's `basicConfig` does nothing because the root logger
    # already has the file handler, so the log keeps going to disk.
    from tokenmeter.__main__ import main as cli

    return cli(sys.argv[1:])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        detail = traceback.format_exc()
        logging.getLogger("tokenmeter").critical("startup failure\n%s", detail)
        try:
            import tkinter.messagebox as mb

            mb.showerror("TokenMeter could not start", detail[-1500:])
        except Exception:
            pass
        sys.exit(1)
