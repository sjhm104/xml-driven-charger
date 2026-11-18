"""程序入口。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # 允许直接运行 python app/main.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ChargerSim")
    app.setOrganizationName("ChargerSim")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())