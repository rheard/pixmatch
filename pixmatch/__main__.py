import argparse
import logging
import platform

from pathlib import Path

from PySide6 import QtWidgets

from pixmatch.gui import MainWindow

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process zero or more file paths.",
    )
    parser.add_argument(
        "folders",
        nargs="*",
        type=Path,
        help="Folders to load into the selected file path display (to speed up testing).",
    )
    parser.add_argument('--verbose', action='store_true', help="More detailed logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(module)s::%(funcName)s::%(lineno)d %(levelname)s %(asctime)s - %(message)s',
    )

    if platform.system() == "Windows":
        # Need to tell Windows to not use the Python app icon and use the Window icon isntead...
        #    The taskbar groups windows by this ID, so with our own ID the app gets its own button instead of Python's.
        #    Microsoft's format is CompanyName.ProductName, with no version so that upgrades keep any taskbar pins.
        # https://stackoverflow.com/questions/1551605
        # https://learn.microsoft.com/en-us/windows/win32/shell/appids
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('RHeard.PixMatch')

    app = QtWidgets.QApplication([])
    w = MainWindow(args.folders)
    w.show()
    app.exec()
