#!/usr/bin/env python3
"""Launch the standalone DSW GUI from source or a frozen Linux build."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

import argparse
from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "dsw"))

from PySide6 import QtWidgets
from app import APP_NAME, Window


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--open", type=Path, help="Open a session or spectrum for offline analysis")
    parser.add_argument("--port", help="Select a serial device path; connection still requires Connect")
    arguments = parser.parse_args()
    application = QtWidgets.QApplication([sys.argv[0]])
    application.setApplicationName(APP_NAME)
    application.setApplicationDisplayName(APP_NAME)
    application.setOrganizationName("DNRI")
    application.setStyle("Fusion")
    window = Window()
    if arguments.port:
        index = window.port.findData(arguments.port)
        if index < 0:
            window.port.addItem(arguments.port, arguments.port)
            index = window.port.count() - 1
        window.port.setCurrentIndex(index)
        window.controls()
    if arguments.open:
        window.open_document(filename=str(arguments.open.resolve()))
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
