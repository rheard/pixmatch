from datetime import datetime, timezone
from enum import Enum, auto
from functools import cache
from pathlib import Path
from typing import Dict, Iterable, List, Sequence
from zipfile import ZipFile

from PySide6 import QtCore, QtGui, QtWidgets

from pixmatch import ZipPath
from pixmatch.gui.utils import NO_MARGIN, MAX_SIZE_POLICY
from pixmatch.utils import human_bytes

ZIP_ICON_PATH = Path(__file__).resolve().parent / 'zip.png'


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


# region Image view panel
def _load_pixmap(path: ZipPath, thumb_size: int) -> QtGui.QPixmap:
    """Load an image from disk and scale to a square thumbnail."""
    if path.subpath:
        with ZipFile(path.path) as zf:
            pm = QtGui.QPixmap()
            pm.loadFromData(zf.read(path.subpath))
    else:
        pm = QtGui.QPixmap(str(path.path))

    if pm.isNull():
        # Fallback: generate a checkerboard if load failed.
        pm = QtGui.QPixmap(thumb_size, thumb_size)
        pm.fill(QtGui.QColor("lightgray"))
        p = QtGui.QPainter(pm)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        c1 = QtGui.QColor(210, 210, 210)
        c2 = QtGui.QColor(180, 180, 180)
        for y in range(0, thumb_size, 16):
            for x in range(0, thumb_size, 16):
                p.setBrush(c1 if ((x // 16 + y // 16) % 2 == 0) else c2)
                p.drawRect(x, y, 16, 16)
        p.end()
    return pm.scaled(thumb_size, thumb_size,
                     QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                     QtCore.Qt.TransformationMode.SmoothTransformation)


def movie_size(movie: QtGui.QMovie):
    movie.jumpToFrame(0)
    rect = QtCore.QRect()
    for i in range(movie.frameCount()):
        movie.jumpToNextFrame()
        rect |= movie.frameRect()
    width = rect.x() + rect.width()
    height = rect.y() + rect.height()

    return QtCore.QSize(width, height)


class ImageViewPane(QtWidgets.QWidget):
    """Container with a stacked image viewer and a bottom overlay status label."""
    def __init__(self, parent=None):
        super().__init__(parent)

        # --- viewers ---
        self.current_path = None
        self._buffer = self._qbytearray = None
        self.scaled = ScaledLabel(contentsMargins=NO_MARGIN, sizePolicy=MAX_SIZE_POLICY)
        self.scaled.setMinimumSize(10, 10)

        self.raw_label = QtWidgets.QLabel()
        self.raw_label.setContentsMargins(NO_MARGIN)
        self.raw_label.setMargin(0)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setContentsMargins(NO_MARGIN)
        self.scroll.setSizePolicy(MAX_SIZE_POLICY)
        self.scroll.setWidget(self.raw_label)

        # Only one visible at a time -> use a stack
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.scaled)   # index 0
        self.stack.addWidget(self.scroll)   # index 1

        # --- overlay status label ---
        self.status = QtWidgets.QLabel(text="Ready", alignment=QtCore.Qt.AlignmentFlag.AlignBottom)
        self.status.setContentsMargins(NO_MARGIN)
        self.status.setObjectName("imageStatus")
        self.status.setMaximumHeight(16)
        # self.status.setStyleSheet("""
        #     QLabel#imageStatus {
        #         font-size: 14px;
        #     }
        # """)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(NO_MARGIN)
        lay.addWidget(self.stack)
        lay.addWidget(self.status)

    # Optional helper you can call to update the text
    def set_status(self, text: str):
        self.status.setText(text)

    def set_index(self, index: int):
        if index not in (0, 1):
            raise ValueError('Valid index must be 0 or 1 for the image pane to select!')

        self.stack.setCurrentIndex(index)

        if self.current_path:
            self.set_image(self.current_path)

        self.update()

    def clear(self):
        """Clear and reset the current object, and the two sub-objects"""
        existing_movie = self.raw_label.movie()
        if existing_movie:
            existing_movie.stop()
            existing_movie.deleteLater()
        self.raw_label.clear()

        self.scaled.clear()

    def set_image(self, path: ZipPath):
        if path == self.current_path:
            return

        self.current_path = path
        file_size = modified = None
        self.clear()
        if path.is_gif:
            # We're setting a movie...
            if path.subpath:
                # Need to load movie from a zipfile
                with ZipFile(path.path) as zf:
                    st = zf.getinfo(path.subpath)
                    modified = st.date_time
                    file_size = st.file_size
                    self._qbytearray = QtCore.QByteArray(zf.read(path.subpath))
                    self._buffer = QtCore.QBuffer(self._qbytearray)
                    self._buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)

                    movie = QtGui.QMovie()
                    movie.setFormat(b'gif')
                    movie.setDevice(self._buffer)
            else:
                # Basic movie path
                movie = QtGui.QMovie(str(path.path))

            object_size = movie_size(movie)

            if self.stack.currentIndex() == 0:
                self.scaled.setMovie(movie)
            else:
                self.raw_label.setMovie(movie)

            movie.start()
        else:
            # We're setting an image...
            if path.subpath:
                # Need to load image from a zipfile
                with ZipFile(path.path) as zf:
                    st = zf.getinfo(path.subpath)
                    modified = st.date_time
                    file_size = st.file_size
                    pixmap = QtGui.QPixmap()
                    pixmap.loadFromData(zf.read(path.subpath))
            else:
                # Basic image path
                pixmap = QtGui.QPixmap(str(path.path))

            object_size = pixmap.size()

            if self.stack.currentIndex() == 0:
                self.scaled.setPixmap(pixmap)
            else:
                self.raw_label.setPixmap(pixmap)

        if self.stack.currentIndex() == 1:
            self.raw_label.resize(object_size)

        self.update()

        # region Update status text
        if not path.subpath:
            path = Path(path.path)
            st = path.stat()
            modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime('%m/%d/%Y')
            file_size = st.st_size
        elif modified:
            modified = f"{modified[1]}/{modified[2]}/{modified[0]}"
        self.status.setText(
            f"{path.absolute()} ("
            f"{human_bytes(file_size)} "
            f"- {object_size.width()},{object_size.height()}px "
            f"- {modified}"
            f")"
        )
        # endregion


class ScaledLabel(QtWidgets.QLabel):
    """
    A version of the above ScaledLabel but for gifs/movies

    https://stackoverflow.com/questions/72188903
    https://stackoverflow.com/questions/77602181
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._movieSize = QtCore.QSize()
        self._minSize = QtCore.QSize()
        self.object_size = QtCore.QSize()
        self.orig_pixmap = self.pixmap()
        self.orig_movie = self.movie()

    def clear(self):
        super().clear()
        self.orig_pixmap = None
        if self.orig_movie:
            self.orig_movie.stop()
            self.orig_movie.deleteLater()
            self.orig_movie = None

    def minimumSizeHint(self):
        if self._minSize.isValid():
            return self._minSize
        return super().minimumSizeHint()

    def setPixmap(self, pixmap):  # overiding setPixmap
        if not pixmap:
            return
        self.clear()
        self.orig_pixmap = pixmap
        return super().setPixmap(self.orig_pixmap.scaled(self.frameSize(), QtCore.Qt.AspectRatioMode.KeepAspectRatio))

    def setMovie(self, movie):
        if self.movie() == movie:
            return
        if self.orig_movie and movie:
            self.clear()
        super().setMovie(movie)
        self.orig_movie = movie

        if not isinstance(movie, QtGui.QMovie) or not movie.isValid():
            self._movieSize = QtCore.QSize()
            self._minSize = QtCore.QSize()
            self.updateGeometry()
            return

        cf = movie.currentFrameNumber()
        movie.jumpToFrame(0)
        self._movieSize = movie_size(movie)
        width = self._movieSize.width()
        height = self._movieSize.height()

        minimum = min(width, height)
        maximum = max(width, height)
        ratio = maximum / minimum
        base = min(4, minimum)
        self._minSize = QtCore.QSize(base, round(base * ratio))
        if minimum == width:
            self._minSize.transpose()

        movie.jumpToFrame(cf)
        self.updateGeometry()

    def paintEvent(self, event):
        movie = self.movie()
        if not isinstance(movie, QtGui.QMovie) or not movie.isValid():
            super().paintEvent(event)
            if self.orig_pixmap:
                self.setPixmap(self.orig_pixmap)
            return

        qp = QtGui.QPainter(self)
        self.drawFrame(qp)

        cr = self.contentsRect()
        margin = self.margin()
        cr.adjust(margin, margin, -margin, -margin)

        style = self.style()
        alignment = style.visualAlignment(self.layoutDirection(), self.alignment())
        maybeSize = self._movieSize.scaled(cr.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

        if maybeSize != movie.scaledSize():
            movie.setScaledSize(maybeSize)
            style.drawItemPixmap(
                qp, cr, alignment,
                movie.currentPixmap().scaled(cr.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            )

        else:
            style.drawItemPixmap(
                qp, cr, alignment,
                movie.currentPixmap()
            )
# endregion


# region Thumbnail tile panel
@cache
def get_overlay_icon(height, width):
    return QtGui.QPixmap(ZIP_ICON_PATH).scaled(
        int(height), int(width),
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.FastTransformation,
    )


class ThumbnailTile(QtWidgets.QFrame):
    """
    Clickable thumbnail tile that cycles between KEEP → DELETE → IGNORE.

    Attributes:
        path: Image path (opaque identifier for the caller).
        stateChanged(path: str, state: SelectionState): Emitted on state updates.
    """
    stateChanged = QtCore.Signal(ZipPath, SelectionState)
    hovered = QtCore.Signal(ZipPath)

    def __init__(self, path: ZipPath, pixmap: QtGui.QPixmap, thumb_size: int = 32, parent=None):
        super().__init__(parent, frameShape=QtWidgets.QFrame.Shape.Box, lineWidth=2)
        self.setObjectName("ThumbnailTile")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        self._path = path
        self._state = SelectionState.KEEP
        self._thumb_size = thumb_size

        self._image = QtWidgets.QLabel(alignment=QtCore.Qt.AlignmentFlag.AlignCenter, pixmap=pixmap)
        self._image.setFixedSize(thumb_size, thumb_size)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(NO_MARGIN)
        lay.setSpacing(0)
        lay.addWidget(self._image)

        if path.subpath:
            _overlay_icon = QtWidgets.QLabel(self._image)  # child of the tile so it floats over the image
            _overlay_icon.setObjectName("LockOverlay")
            _overlay_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            _overlay_icon.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            _overlay_icon.setFixedSize(thumb_size, thumb_size)  # small badge; adjust later if you want
            _overlay_icon.setPixmap(get_overlay_icon(thumb_size / 1.5, thumb_size / 1.5))

        self._apply_state_style()

    @property
    def path(self) -> ZipPath:
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
        locked = bool(self._path.subpath)
        next_state = STATE_ORDER[(idx + 1) % len(STATE_ORDER)]
        if locked and next_state == SelectionState.DELETE:
            next_state = STATE_ORDER[(idx + 2) % len(STATE_ORDER)]
        self.state = next_state

    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.MouseButton.LeftButton:
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
    tileStateChanged = QtCore.Signal(ZipPath, SelectionState)
    tileHovered = QtCore.Signal(ZipPath)

    def __init__(self, images: Sequence[ZipPath], thumb_size: int = 32, parent=None):
        super().__init__(parent)
        self._tiles: List[ThumbnailTile] = []
        self._thumb_size = thumb_size
        self.layout = QtWidgets.QHBoxLayout(self)
        self.layout.setContentsMargins(NO_MARGIN)
        self.layout.setSpacing(0)

        for path in images:
            self.add_tile(path)

        self.layout.addStretch(1)

    def tiles(self) -> Iterable[ThumbnailTile]:
        return list(self._tiles)

    def add_tile(self, path: ZipPath):
        pm = _load_pixmap(path, self._thumb_size)
        tile = ThumbnailTile(path=path, pixmap=pm, thumb_size=self._thumb_size)
        tile.stateChanged.connect(self.tileStateChanged)
        tile.hovered.connect(self.tileHovered)
        self._tiles.append(tile)
        self.layout.insertWidget(len(self._tiles) - 1, tile)


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

    groupTileStateChanged = QtCore.Signal(ZipPath, SelectionState)  # path, state
    groupTileHovered = QtCore.Signal(ZipPath)

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

    def set_groups(self, groups: Sequence[Sequence[ZipPath]]) -> None:
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

    def add_group(self, group: Sequence[ZipPath]) -> None:
        if len(self._rows) == self._max_rows:
            raise ValueError("Cannot add a new group to a fully filled group list!")

        row = DuplicateGroupRow(group, thumb_size=self._thumb_size)
        row.tileStateChanged.connect(self.groupTileStateChanged)
        row.tileHovered.connect(self.groupTileHovered)
        tail_index = self._vbox.count() - 1
        self._vbox.insertWidget(tail_index, row)
        self._rows.append(row)

    def decisions(self) -> Dict[ZipPath, SelectionState]:
        """Collect {path: state} for all tiles across all rows."""
        out: Dict[ZipPath, SelectionState] = {}
        for row in self._rows:
            for tile in row.tiles():
                out[tile.path] = tile.state
        return out

    def reset_states(self) -> None:
        """Set all tiles to KEEP."""
        for row in self._rows:
            for tile in row.tiles():
                tile.state = SelectionState.KEEP

    def _clear_rows(self) -> None:
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
# endregion


class DirFileSystemModel(QtWidgets.QFileSystemModel):
    def hasChildren(self, /, parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = ...):
        file_info = self.fileInfo(parent)
        _dir = QtCore.QDir(file_info.absoluteFilePath())
        return bool(_dir.entryList(self.filter()))
