import argparse

from pathlib import Path

from PySide6 import QtWidgets

from pixmatch.gui import MainWindow


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process zero or more file paths."
    )
    parser.add_argument(
        "folders",
        nargs="*",
        type=Path,
        help="Folders to load into the selected file path display (to speed up testing).",
    )
    args = parser.parse_args()

    app = QtWidgets.QApplication([])
    # Basic stylesheet for subtle polish without complexity.
    app.setStyleSheet(
        """
        QToolBar { spacing: 8px; }
        QLabel#GroupTitle { padding: 4px 0; }
        QFrame#ImageTile { border: 1px solid #444; border-radius: 6px; padding: 6px; }
        """
    )
    w = MainWindow(args.folders)
    w.show()
    app.exec()
