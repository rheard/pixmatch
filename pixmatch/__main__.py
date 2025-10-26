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
        #    I'm not sure on the specifics but calling this method with any string seems to do the trick....
        # https://stackoverflow.com/questions/1551605
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('company.app.1')

    app = QtWidgets.QApplication([])
    # Basic stylesheet for subtle polish without complexity.
    app.setStyleSheet(
        """
        QToolBar { spacing: 8px; }
        QLabel#GroupTitle { padding: 4px 0; }
        QFrame#ImageTile { border: 1px solid #444; border-radius: 6px; padding: 6px; }
        """,
    )
    w = MainWindow(args.folders)
    w.show()
    app.exec()
