"""Generates `tokenmeter/assets/tokenmeter.ico` for the shortcut and the .exe.

    python make_icon.py

Only rerun it when the icon design in `ui/icon.py` changes.
"""

from pathlib import Path

from tokenmeter.ui.icon import app_icon, write_ico

ASSETS = Path(__file__).parent / "tokenmeter" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

ico = ASSETS / "tokenmeter.ico"
write_ico(ico)
app_icon(256).save(ASSETS / "tokenmeter.png")

print(f"{ico}  ({ico.stat().st_size:,} bytes)")
print(f"{ASSETS / 'tokenmeter.png'}")
