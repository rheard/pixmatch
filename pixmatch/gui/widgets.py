import os

from enum import Enum, auto
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from PIL import Image
from PySide6 import QtCore, QtGui, QtWidgets

from pixmatch.gui.utils import NO_MARGIN, MAX_SIZE_POLICY


class SelectionState(Enum):
    """Per-thumbnail action state."""
    KEEP = auto()
    DELETE = auto()
    IGNORE = auto()


STATE_ORDER = [SelectionState.KEEP, SelectionState.DELETE, SelectionState.IGNORE]
STATE_COLORS = {
    SelectionState.KEEP: QtGui.QColor(80, 200, 120),     # green
    SelectionState.DELETE: QtGui.QColor(230, 80, 80),    # red
    SelectionState.IGNORE: QtGui.QColor(240, 190, 60),   # amber
}


def _load_pixmap(path: Path | str, thumb_size: int) -> QtGui.QPixmap:
    """Load an image from disk and scale to a square thumbnail."""
    pm = QtGui.QPixmap(path)
    if pm.isNull():
        # Fallback: generate a checkerboard if load failed.
        pm = QtGui.QPixmap(thumb_size, thumb_size)
        pm.fill(QtGui.QColor("lightgray"))
        p = QtGui.QPainter(pm)
        p.setPen(QtCore.Qt.NoPen)
        c1 = QtGui.QColor(210, 210, 210)
        c2 = QtGui.QColor(180, 180, 180)
        for y in range(0, thumb_size, 16):
            for x in range(0, thumb_size, 16):
                p.setBrush(c1 if ((x // 16 + y // 16) % 2 == 0) else c2)
                p.drawRect(x, y, 16, 16)
        p.end()
    return pm.scaled(thumb_size, thumb_size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)


class ImageViewPane(QtWidgets.QWidget):
    """Container with a stacked image viewer and a bottom overlay status label."""
    def __init__(self, parent=None):
        super().__init__(parent)

        # --- viewers ---
        self.scaled = ScaledLabel(contentsMargins=NO_MARGIN, sizePolicy=MAX_SIZE_POLICY)
        self.scaled.setMinimumSize(10, 10)

        self.raw_label = QtWidgets.QLabel(contentsMargins=NO_MARGIN)
        self.scroll = QtWidgets.QScrollArea(contentsMargins=NO_MARGIN, widget=self.raw_label,
                                            sizePolicy=MAX_SIZE_POLICY)

        # Only one visible at a time -> use a stack
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.scaled)   # index 0
        self.stack.addWidget(self.scroll)   # index 1

        # --- overlay status label ---
        self.status = QtWidgets.QLabel(contentsMargins=NO_MARGIN, objectName="imageStatus", text="Ready",
                                       maximumHeight=16, alignment=QtCore.Qt.AlignmentFlag.AlignBottom)
        # self.status.setStyleSheet("""
        #     QLabel#imageStatus {
        #         font-size: 14px;
        #     }
        # """)

        lay = QtWidgets.QVBoxLayout(self, contentsMargins=NO_MARGIN)
        lay.addWidget(self.stack)
        lay.addWidget(self.status)

    # Optional helper you can call to update the text
    def set_status(self, text: str):
        self.status.setText(text)


class ThumbnailTile(QtWidgets.QFrame):
    """
    Clickable thumbnail tile that cycles between KEEP → DELETE → IGNORE.

    Attributes:
        path: Image path (opaque identifier for the caller).
        stateChanged(path: str, state: SelectionState): Emitted on state updates.
    """
    stateChanged = QtCore.Signal(str, SelectionState)
    hovered = QtCore.Signal(str)

    def __init__(self, path: Path | str, pixmap: QtGui.QPixmap, thumb_size: int = 32, parent=None):
        super().__init__(parent, objectName="ThumbnailTile", frameShape=QtWidgets.QFrame.Box, lineWidth=2,
                         cursor=QtCore.Qt.PointingHandCursor)

        self._path = path
        self._state = SelectionState.KEEP
        self._thumb_size = thumb_size

        self._image = QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter, pixmap=pixmap)
        self._image.setFixedSize(thumb_size, thumb_size)

        lay = QtWidgets.QVBoxLayout(self, contentsMargins=NO_MARGIN, spacing=0)
        lay.addWidget(self._image, alignment=QtCore.Qt.AlignCenter)

        self._apply_state_style()

    @property
    def path(self) -> str:
        return self._path

    @property
    def state(self) -> SelectionState:
        return self._state

    @state.setter
    def state(self, state: SelectionState) -> None:
        """Set the tile selection state without cycling."""
        if self._state is state:
            return
        self._state = state
        self._apply_state_style()
        self.stateChanged.emit(self._path, self._state)

    def cycle_state(self) -> None:
        """Advance KEEP → DELETE → IGNORE → KEEP."""
        idx = STATE_ORDER.index(self._state)
        self.state = STATE_ORDER[(idx + 1) % len(STATE_ORDER)]

    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.LeftButton:
            self.cycle_state()
            e.accept()
        else:
            super().mousePressEvent(e)

    def enterEvent(self, e: QtGui.QEnterEvent) -> None:
        # fire when the cursor enters the tile
        self.hovered.emit(self._path)
        super().enterEvent(e)

    def _apply_state_style(self) -> None:
        color = STATE_COLORS[self._state]
        self.setStyleSheet(
            f"""
            QFrame#ThumbnailTile {{
                border: 2px solid rgba({color.red()}, {color.green()}, {color.blue()}, 220);
                border-radius: 6px;
                background: #202020;
            }}
            QLabel#StateBadge {{
                color: black;
                background: rgba({color.red()}, {color.green()}, {color.blue()}, 220);
                border-radius: 6px;
                font-weight: 600;
            }}
            """
        )


class DuplicateGroupRow(QtWidgets.QWidget):
    """
    A single row of thumbnails representing one duplicate group.

    Signals:
        tileStateChanged(path: str, state: SelectionState)
    """
    tileStateChanged = QtCore.Signal(str, SelectionState)
    tileHovered = QtCore.Signal(str)

    def __init__(self, images: Sequence[Path | str], thumb_size: int = 32, parent=None):
        super().__init__(parent)
        self._tiles: List[ThumbnailTile] = []
        self._thumb_size = thumb_size
        self.layout = QtWidgets.QHBoxLayout(self, contentsMargins=NO_MARGIN, spacing=0)

        for path in images:
            self.add_tile(path)

        self.layout.addStretch(1)

    def tiles(self) -> Iterable[ThumbnailTile]:
        return list(self._tiles)

    def add_tile(self, path: Path | str):
        pm = _load_pixmap(path, self._thumb_size)
        tile = ThumbnailTile(path=path, pixmap=pm, thumb_size=self._thumb_size)
        tile.stateChanged.connect(self.tileStateChanged)
        tile.hovered.connect(self.tileHovered)
        self._tiles.append(tile)
        self.layout.addWidget(tile)


class DuplicateGroupList(QtWidgets.QWidget):
    """
    Scrollable list of duplicate groups. Each group renders as a row of thumbnails.

    Public API:
        set_groups(groups): Load groups; each group is a list of image paths.
        decisions(): Dict[path, SelectionState] for all tiles.
        set_max_rows(n): Limit how many groups to show (default 25).
        set_thumb_size(px): Set square thumbnail size (default 128).
        reset_states(): Set all tiles to KEEP.

    Notes:
        - Clicking a thumbnail cycles KEEP → DELETE → IGNORE.
        - Borders/badges are colored by state.
    """

    groupTileStateChanged = QtCore.Signal(str, SelectionState)  # path, state
    groupTileHovered = QtCore.Signal(str)

    def __init__(self, parent=None, *, max_rows: int = 25, thumb_size: int = 64, **kwargs):
        super().__init__(parent, **kwargs)
        self._max_rows = max_rows
        self._thumb_size = thumb_size

        self._scroll = QtWidgets.QScrollArea(widgetResizable=True)
        self._container = QtWidgets.QWidget()
        self._vbox = QtWidgets.QVBoxLayout(self._container)
        self._vbox.setContentsMargins(NO_MARGIN)
        self._vbox.setSpacing(0)
        _tail_spacer = QtWidgets.QSpacerItem(
            0, 0, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding
        )
        self._vbox.addItem(_tail_spacer)
        self._scroll.setWidget(self._container)

        # Header with quick-actions.
        # self._header = QtWidgets.QHBoxLayout(contentsMargins=NO_MARGIN)
        # self._btn_keep_all = QtWidgets.QPushButton("Mark All Keep", contentsMargins=NO_MARGIN)
        # self._btn_delete_all = QtWidgets.QPushButton("Mark All Delete", contentsMargins=NO_MARGIN)
        # self._btn_ignore_all = QtWidgets.QPushButton("Mark All Ignore", contentsMargins=NO_MARGIN)
        # self._header.addWidget(self._btn_keep_all)
        # self._header.addWidget(self._btn_delete_all)
        # self._header.addWidget(self._btn_ignore_all)
        # self._header.addStretch(1)

        # self._btn_keep_all.clicked.connect(lambda: self._bulk_set(SelectionState.KEEP))
        # self._btn_delete_all.clicked.connect(lambda: self._bulk_set(SelectionState.DELETE))
        # self._btn_ignore_all.clicked.connect(lambda: self._bulk_set(SelectionState.IGNORE))

        # Main layout
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(NO_MARGIN)
        outer.setSpacing(0)
        # self._outer.addLayout(self._header)
        outer.addWidget(self._scroll)

        self._rows: List[DuplicateGroupRow] = []

        # Status bar
        _status = QtWidgets.QHBoxLayout()
        self.left_arrow = QtWidgets.QPushButton("<")
        self.page_indicator = QtWidgets.QLabel(alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        self.right_arrow = QtWidgets.QPushButton(">")
        _status.addWidget(self.left_arrow)
        _status.addWidget(self.page_indicator)
        _status.addWidget(self.right_arrow)
        outer.addLayout(_status)

        self.update_page_indicator(1, 1)

    def set_max_rows(self, n: int) -> None:
        """Set maximum visible rows (groups)."""
        self._max_rows = max(1, int(n))

    def set_thumb_size(self, px: int) -> None:
        """Set square thumbnail size for subsequent loads."""
        self._thumb_size = max(32, int(px))

    def set_groups(self, groups: Sequence[Sequence[Path | str]]) -> None:
        """
        Load duplicate groups.

        Args:
            groups: An iterable of groups; each group is an iterable of image file paths.
                    Only the first `max_rows` groups are shown.
        """
        self._clear_rows()
        for group in groups[: self._max_rows]:
            self.add_group(group)

    def update_page_indicator(self, current_page, total_pages):
        self.page_indicator.setText(f"Page {current_page} of {total_pages}")

    def add_group(self, group: Sequence[Path | str]) -> None:
        if len(self._rows) == self._max_rows:
            raise ValueError("Cannot add a new group to a fully filled group list!")

        group = [str(x) for x in group]
        row = DuplicateGroupRow(group, thumb_size=self._thumb_size)
        row.tileStateChanged.connect(self.groupTileStateChanged)
        row.tileHovered.connect(self.groupTileHovered)
        tail_index = self._vbox.count() - 1
        self._vbox.insertWidget(tail_index, row)
        self._rows.append(row)

    def decisions(self) -> Dict[str, SelectionState]:
        """Collect {path: state} for all tiles across all rows."""
        out: Dict[str, SelectionState] = {}
        for row in self._rows:
            for tile in row.tiles():
                out[tile.path] = tile.state
        return out

    def reset_states(self) -> None:
        """Set all tiles to KEEP."""
        for row in self._rows:
            for tile in row.tiles():
                tile.state = SelectionState.KEEP

    # --- internals -----------------------------------------------------------------

    def _clear_rows(self) -> None:
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

    def _on_tile_state_changed(self, path: str, state: SelectionState) -> None:
        self.groupTileStateChanged.emit(path, state)


# -----------------------------------------------------------------------------
# Main Window
# -----------------------------------------------------------------------------


class DirFileSystemModel(QtWidgets.QFileSystemModel):
    def hasChildren(self, /, parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = ...):
        file_info = self.fileInfo(parent)
        _dir = QtCore.QDir(file_info.absoluteFilePath())
        return bool(_dir.entryList(self.filter()))


class ScaledLabel(QtWidgets.QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orig_pixmap = self.pixmap()

    def resizeEvent(self, event):
        self.setPixmap(self.orig_pixmap)

    def setPixmap(self, pixmap):  # overiding setPixmap
        if not pixmap:
            return
        self.orig_pixmap = pixmap
        return super().setPixmap(self.orig_pixmap.scaled(self.frameSize(), QtCore.Qt.KeepAspectRatio))