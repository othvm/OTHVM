"""Install or remove the desktop pet from the current user's startup.

Uses ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` so no
administrator rights are needed.  The stored command points at the repository's
``pythonw.exe`` and ``app.py``; it contains no secrets.

Usage:
    python desktop_pet/autostart.py status
    python desktop_pet/autostart.py install [--hidden]
    python desktop_pet/autostart.py remove
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import winreg
except Exception:  # pragma: no cover - non-Windows
    winreg = None

BASE_DIR = Path(__file__).resolve().parent
APP_PATH = BASE_DIR / "app.py"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "OTHVM Desktop Pet"


def _pythonw() -> Path:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def command(hidden: bool = False) -> str:
    suffix = " --hidden" if hidden else ""
    return f'"{_pythonw()}" "{APP_PATH}"{suffix}'


def install(hidden: bool = False) -> str:
    if winreg is None:
        raise RuntimeError("winreg is only available on Windows")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command(hidden))
    return command(hidden)


def remove() -> bool:
    if winreg is None:
        raise RuntimeError("winreg is only available on Windows")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False


def status() -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "install", "remove"))
    parser.add_argument("--hidden", action="store_true",
                        help="start with the control panel hidden")
    args = parser.parse_args()

    if args.action == "install":
        print("installed: " + install(hidden=args.hidden))
        return 0
    if args.action == "remove":
        print("removed" if remove() else "not installed")
        return 0
    current = status()
    print(current if current else "not installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
