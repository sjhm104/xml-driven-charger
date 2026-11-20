"""运行路径：源码运行时用工程根目录，PyInstaller 打包后用 exe 所在目录。"""

from __future__ import annotations

from pathlib import Path
import sys


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent