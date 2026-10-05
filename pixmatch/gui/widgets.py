import io
import os
import queue
import stat
import sys
import threading
import time

from collections import OrderedDict, defaultdict
from contextlib import ExitStack, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import cache
from itertools import count
from pathlib import Path
from typing import Sequence
from zipfile import BadZipFile, ZipFile

import numpy as np

from PIL import Image
from PySide6 import QtCore, QtGui, QtWidgets

from pixmatch import ZipPath
from pixmatch.gui.utils import MAX_SIZE_POLICY, NO_MARGIN
from pixmatch.utils import human_bytes

ZIP_ICON_PATH = Path(__file__).resolve().parent / 'zip.png'

PREVIEW_CACHE_BYTES = 512 * 1024 * 1024  # The most memory the previews kept for showing again can take up
PREFETCH_LIMIT = 8  # The most images of a hovered tile's group whose previews are read ahead of being hovered


class SelectionState(Enum):
    """Per-thumbnail action state."""
    KEEP = 'keep'
    DELETE = 'delete'
    IGNORE = 'ignore'
    MOVE = 'move'


@dataclass(frozen=True)
class SelectionValue:
    """A selection state with any arguments"""
    state: SelectionState
    args: tuple = ()


class SelectionValues:
    """Similar to the SelectionState enum, but for basic SelectionValue instances"""
    KEEP = SelectionValue(SelectionState.KEEP)
    DELETE = SelectionValue(SelectionState.DELETE)
    IGNORE = SelectionValue(SelectionState.IGNORE)


STATE_ORDER = [SelectionValues.KEEP, SelectionValues.DELETE, SelectionValues.IGNORE]
STATE_COLORS = {
    SelectionState.KEEP: QtGui.QColor(80, 200, 120),     # green
    SelectionState.DELETE: QtGui.QColor(230, 80, 80),    # red
    SelectionState.IGNORE: QtGui.QColor(240, 190, 60),   # amber
    SelectionState.MOVE: QtGui.QColor(130, 200, 255),    # blue
}


# region Image view panel
def _load_thumbnail(path: ZipPath, thumb_size: int, zf: ZipFile | None = None) -> QtGui.QImage:
    """
    Load an image from disk as a square thumbnail.

    The image is decoded straight to the thumbnail's size, which lets JPEGs skip most of the work of decoding them
        in full (a 4K JPEG took ~78ms to load at full size, and ~19ms this way).
    If the image is in a zip that is already open, pass it as zf so it isn't opened again.

    Returns:
        QtGui.QImage: The thumbnail, which is null if the image couldn't be read.
    """
    if path.subpath:
        with nullcontext(zf) if zf is not None else ZipFile(path.path) as zip_file:
            return _decode_thumbnail(zip_file.read(path.subpath), thumb_size)

    reader = QtGui.QImageReader(path.path)
    reader.setScaledSize(QtCore.QSize(thumb_size, thumb_size))
    return reader.read()


def _decode_thumbnail(data: bytes, thumb_size: int) -> QtGui.QImage:
    """
    Decode an image's data (a file in a zip) as a square thumbnail, with Pillow.

    Qt could only read this data through a QBuffer made in Python, and that deadlocked the app: while Qt works out how
        to read an image it holds a lock and calls the buffer, and PySide needs the GIL for those calls. So a thread
        loading a thumbnail could hold the lock waiting for the GIL, while the GUI thread held the GIL waiting for the
        lock to load an image of its own (like the preview of an image being hovered over).

    Returns:
        QtGui.QImage: The thumbnail, which is null if the image couldn't be read (like QImageReader's).
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (thumb_size, thumb_size))  # JPEGs decode at a fraction of their size, as with QImageReader
            if im.mode.startswith("I;16"):
                # Pillow clips 16-bit values to 0-255 when converting, which turns nearly everything white
                im = Image.fromarray((np.asarray(im) >> 8).astype(np.uint8))
            thumbnail = im.convert("RGBA").resize((thumb_size, thumb_size), Image.Resampling.BILINEAR)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return QtGui.QImage()

    # Copied so the QImage has its own pixels, instead of pointing at the bytes (which are about to be freed)
    return QtGui.QImage(thumbnail.tobytes(), thumb_size, thumb_size, QtGui.QImage.Format.Format_RGBA8888).copy()


def _decode_image(data: bytes) -> QtGui.QImage:
    """
    Decode an image's data (a file in a zip) for the preview, with Pillow, off the GUI thread.

    Qt could only read this data through a QBuffer made in Python, which deadlocks off the GUI thread (see
        _decode_thumbnail), or with QImage.fromData, which holds the GIL until it's done, stalling the GUI meanwhile.

    Returns:
        QtGui.QImage: The image, which is null if it couldn't be read (like QImageReader's).
    """
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.mode.startswith("I;16"):
                # Pillow clips 16-bit values to 0-255 when converting, which turns nearly everything white
                im = Image.fromarray((np.asarray(im) >> 8).astype(np.uint8))
            # In Qt's own 32-bit formats, so making a QPixmap from it doesn't convert every pixel again
            if im.mode in {"RGBA", "RGBa", "LA", "La", "PA"} or "transparency" in im.info:
                im, raw_mode, image_format = im.convert("RGBA"), "BGRA", QtGui.QImage.Format.Format_ARGB32
            else:
                im, raw_mode, image_format = im.convert("RGB"), "BGRX", QtGui.QImage.Format.Format_RGB32
            width, height = im.size
            pixels = im.tobytes("raw", raw_mode)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return QtGui.QImage()

    # Copied so the QImage has its own pixels, instead of pointing at the bytes (which are about to be freed)
    return QtGui.QImage(pixels, width, height, image_format).copy()


def movie_sizes(movie: QtGui.QMovie):
    """Get the dimensions of a QMovie, and uncompressed file size"""
    # Every frame is drawn onto the same canvas, so the first frame has the size of them all. Measuring each frame
    #   meant decoding the whole animation (twice per hover), which froze the GUI for ~0.4s per 100 800x500 frames
    movie.jumpToFrame(0)
    first_frame = movie.currentImage()
    return first_frame.size(), first_frame.sizeInBytes() * max(movie.frameCount(), 1)


def close_movie(movie: QtGui.QMovie):
    """Stop a movie that's no longer shown, and delete it (with the buffer it plays from) once Qt is done with it"""
    movie.device().close()
    movie.stop()
    movie.deleteLater()


@dataclass
class _Preview:
    """
    An image read for the preview, with the details about its file that are shown under it.

    An animation keeps its file's data, and plays from that in memory. Playing it from its file kept the file open (and
        Windows won't delete or move an open file), and read the file again whenever it was shown.
    """
    path: str  # The path to show
    file_size: int | None = None
    modified: str | None = None  # As it's shown
    image: QtGui.QImage | None = None  # A still image as it was read. Only the GUI thread can make it a QPixmap
    pixmap: QtGui.QPixmap | None = None  # A still image ready to show, see ready
    movie_data: QtCore.QByteArray | None = None  # An animation's file
    error: str = ''  # Why it couldn't be read

    @property
    def cost(self) -> int:
        """About how much memory this takes up"""
        if self.pixmap is not None:
            return self.pixmap.width() * self.pixmap.height() * self.pixmap.depth() // 8
        if self.movie_data is not None:
            return self.movie_data.size()
        return 0

    def ready(self):
        """Make a still image into a QPixmap to show. This must be done on the GUI thread"""
        if self.image is not None:
            self.pixmap = QtGui.QPixmap.fromImage(self.image)
            self.image = None


def _read_preview(path: ZipPath) -> _Preview:
    """
    Read an image for the preview, still or animated, with the details about its file.

    This runs on PreviewLoader's threads, so it only reads images with Qt from a path, and with Pillow from data in
        memory (see _decode_image).

    Returns:
        _Preview: The image, or why it couldn't be read.
    """
    try:
        if path.subpath:
            with ZipFile(path.path) as zf:
                info = zf.getinfo(path.subpath)
                data = zf.read(info)
            year, month, day, *_ = info.date_time
            preview = _Preview(str(path.absolute()), info.file_size, f"{month}/{day}/{year}")
            if path.is_gif:
                preview.movie_data = QtCore.QByteArray(data)
            else:
                preview.image = _decode_image(data)
        else:
            file = Path(path.path)
            st = file.stat()
            modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime('%m/%d/%Y')
            preview = _Preview(str(file.absolute()), st.st_size, modified)
            if path.is_gif:
                preview.movie_data = QtCore.QByteArray(file.read_bytes())
            else:
                preview.image = QtGui.QImageReader(path.path).read()
    except (OSError, KeyError, BadZipFile) as e:
        # e.g. a file that was changed or deleted outside of the app
        return _Preview(str(path), error=str(e))

    return preview


class PreviewCache:
    """
    The previews shown most recently, so that going back to one shows it straight away.

    Up to `budget` bytes of them are kept: still images are kept decoded, and animations as their file's data.

    Args:
        budget (int): The most memory the previews can take up, in bytes.
    """

    def __init__(self, budget: int = PREVIEW_CACHE_BYTES):
        self._budget = budget
        self._size = 0
        self._previews = OrderedDict()  # ZipPath -> _Preview, least recently used first

    def get(self, path: ZipPath) -> _Preview | None:
        """Get a preview, if it's kept"""
        preview = self._previews.get(path)
        if preview is not None:
            self._previews.move_to_end(path)
        return preview

    def add(self, path: ZipPath, preview: _Preview):
        """Make a preview that was just read ready to show, and keep it (unless it couldn't be read or is too big)"""
        preview.ready()
        if preview.error or (preview.pixmap is not None and preview.pixmap.isNull()) or preview.cost > self._budget:
            # Images that couldn't be read aren't kept, so they're tried again the next time they're shown
            return

        self.forget(path)
        self._previews[path] = preview
        self._size += preview.cost
        while self._size > self._budget:
            _, oldest = self._previews.popitem(last=False)
            self._size -= oldest.cost

    def forget(self, path: ZipPath):
        """Forget a preview, e.g. because its file was deleted or moved"""
        preview = self._previews.pop(path, None)
        if preview is not None:
            self._size -= preview.cost


class _PreviewJob(QtCore.QRunnable):
    """Reads a preview on a PreviewLoader's thread pool"""

    def __init__(self, path: ZipPath, number: int, started: set, read: QtCore.SignalInstance):
        super().__init__()
        self.path = path
        self.number = number  # Which request this is for
        self.started = started  # The loader's set of the requests that have started being read
        self.read = read

    def run(self):
        """Read the preview, and hand it to the GUI thread"""
        self.started.add(self.number)
        try:
            preview = _read_preview(self.path)
        except Exception as e:  # ruff: ignore[blind-except]
            # Anything unexpected (like running out of memory) mustn't leave the preview waiting forever
            preview = _Preview(str(self.path), error=f"{type(e).__name__}: {e}")

        with suppress(RuntimeError):  # The loader was deleted, i.e. the app is closing
            self.read.emit(self.path, self.number, preview)


class PreviewLoader(QtCore.QObject):
    """
    Reads previews on a thread pool, and keeps the ones shown most recently (see PreviewCache).

    Reading a preview on the GUI thread froze the app until the image was read and decoded: ~80-160ms for a large photo
        on a local disk, plus however long reading the file takes from a network drive.

    A preview is read while its file is open, and Windows won't delete or move an open file. So call `wait` first.

    Signals:
        loaded(path: ZipPath, preview: _Preview): A preview asked for with `request` has been read (or couldn't be).
    """
    loaded = QtCore.Signal(ZipPath, object)
    _read = QtCore.Signal(ZipPath, int, object)  # Emitted on the pool's threads, handled on the GUI's

    def __init__(self, parent=None, *, threads: int = 2):
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(threads)
        self._cache = PreviewCache()
        # Path -> the number of the request whose preview is wanted. A request's preview is dropped if its number isn't
        #   here when it's read: it was cancelled (see cancel), or its file was deleted or moved (see forget)
        self._wanted = {}
        self._started = set()  # The numbers of the requests that have started being read
        self._numbers = count(1)
        self._read.connect(self._on_read)

        # The pool waits for every queued preview when it's destroyed, so don't make closing wait for them too
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)

    def get(self, path: ZipPath) -> _Preview | None:
        """Get a preview, if it's kept"""
        return self._cache.get(path)

    def request(self, path: ZipPath, *, priority: int = 1):
        """Start reading a preview, unless it's kept or being read already. `loaded` is emitted once it's read"""
        if path in self._wanted or self._cache.get(path) is not None:
            return

        number = next(self._numbers)
        self._wanted[path] = number
        self._pool.start(_PreviewJob(path, number, self._started, self._read), priority)

    def cancel(self):
        """Drop the requests that haven't started being read yet, e.g. because what they were for isn't shown anymore"""
        self._pool.clear()
        self._wanted = {path: number for path, number in self._wanted.items() if number in self._started}

    def wait(self):
        """Drop the requests that haven't started being read, and wait for the rest, so none of their files are open"""
        self.cancel()
        self._pool.waitForDone()

    def forget(self, path: ZipPath):
        """Forget a file's preview, and drop any read of it in progress, because the file was deleted or moved"""
        self._wanted.pop(path, None)
        self._cache.forget(path)

    def _on_read(self, path: ZipPath, number: int, preview: _Preview):
        """Keep a preview that was read, and pass it on, if it's still wanted"""
        self._started.discard(number)
        if self._wanted.get(path) != number:
            return

        del self._wanted[path]
        self._cache.add(path, preview)
        self.loaded.emit(path, preview)


class ImageViewPane(QtWidgets.QWidget):
    """Container with a stacked image viewer and a bottom overlay status label."""

    def __init__(self, parent=None):
        # This works by having 2 image labels depending on which scaling option was selected
        #   First is the simple scaled label handling scaling
        #   Second is a scroll area with a no-scaling label
        # Then a status label at the bottom
        super().__init__(parent)

        # --- viewers ---
        self.current_path = None
        self._shown = False  # Whether current_path's image is shown, or still being read
        self._previews = PreviewLoader(self)
        self._previews.loaded.connect(self._on_loaded)
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
        self.stack.addWidget(self.scaled)
        self.stack.addWidget(self.scroll)

        self.status = ElidedLabel(text="Ready", alignment=QtCore.Qt.AlignmentFlag.AlignBottom)
        self.status.setContentsMargins(NO_MARGIN)
        self.status.setObjectName("imageStatus")
        self.status.setMaximumHeight(16)
        self.status.setStyleSheet("""
            QLabel#imageStatus {
                font-weight: bold;
            }
        """)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(NO_MARGIN)
        lay.addWidget(self.stack)
        lay.addWidget(self.status)

    def set_status(self, text: str):
        """Set text of the status bar"""
        self.status.setText(text)

    def set_index(self, index: int):
        """Set the index of which image view to use. 0 for scaled label, 1 for scroll area"""
        if index not in (0, 1):
            raise ValueError('Valid index must be 0 or 1 for the image pane to select!')

        self.stack.setCurrentIndex(index)

        # Show the image again, in the view that is shown now. Clearing first takes it out of the other view (where a
        #   movie would keep playing), and keeps set_image from skipping it as the image that's already shown
        path = self.current_path
        self.clear()
        if path:
            self.set_image(path)

        self.update()

    def clear(self):
        """Clear both views, so no image is shown or current"""
        self.current_path = None
        self._shown = False
        self._previews.cancel()  # The previews waiting to be read were for what was shown before

        # Unlike ScaledLabel, a plain QLabel leaves its movie playing when cleared
        movie = self.raw_label.movie()
        self.raw_label.clear()
        if movie:
            close_movie(movie)
        self.scaled.clear()

    def forget(self, path: ZipPath):
        """Forget a file's preview, because the file was deleted or moved"""
        self._previews.forget(path)

    def release_files(self):
        """
        Make sure no file is open for a preview: stop the previews waiting to be read, and wait for the ones being read.

        Call this before deleting or moving files: Windows won't delete or move a file that's open.
        """
        self._previews.wait()

    def prefetch(self, paths: Sequence[ZipPath]):
        """Start reading previews that are likely to be shown next, after the current image's (if it's being read)"""
        for path in paths[:PREFETCH_LIMIT]:
            self._previews.request(path, priority=0)

    def set_image(self, path: ZipPath):
        """Set the image to show in the image view area. Can be an animated image."""
        if path == self.current_path:
            return

        self.clear()
        self.current_path = path
        preview = self._previews.get(path)
        if preview is None:
            # It's shown once it has been read, by _on_loaded
            self.status.set_elided_text(str(path), " (loading...)")
            self._previews.request(path)
            return

        self._show(preview)

    def _on_loaded(self, path: ZipPath, preview: _Preview):
        """A preview has been read, so show it if it's for the image that's current"""
        if path == self.current_path and not self._shown:
            self._show(preview)

    def _show(self, preview: _Preview):
        """Show the preview of the current image"""
        self._shown = True
        if preview.error:
            self.status.set_elided_text(preview.path, f" (couldn't be read: {preview.error})")
            return

        extra = ''
        if preview.movie_data is not None:
            label = self.scaled if self.stack.currentIndex() == 0 else self.raw_label
            # The movie belongs to the label that shows it, and its buffer belongs to the movie, so that neither one can
            #   be deleted while something still uses it. A buffer kept by Python could be deleted before its movie
            #   (e.g. as the app closed), and the movie crashed the app reading its next frame from it.
            #   setData shares the file's data with the preview (QByteArrays copy by sharing), rather than copying it
            movie = QtGui.QMovie(label)
            buffer = QtCore.QBuffer(movie)
            buffer.setData(preview.movie_data)
            buffer.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
            movie.setDevice(buffer)
            object_size, uncompressed_size = movie_sizes(movie)
            label.setMovie(movie)

            # WEBP files which aren't animated will appear as movies with a single frame
            #   Thats boring. For the purposes of the statusbar, just treat them as images
            # TODO: Gee, that makes me wonder...
            #   could any image be treated as a movie and we could do away with this whole pixmap or movie thing?
            #   Worth investigating when I have more time...
            if movie.frameCount() > 1:
                extra = f'({human_bytes(uncompressed_size)}, {movie.frameCount()}) '
            else:
                extra = f'({human_bytes(uncompressed_size)}) '
            movie.start()
        else:
            # Not an animated image, just a normal boring pixmap
            pixmap = preview.pixmap
            extra = f'({human_bytes(pixmap.toImage().sizeInBytes())}) '
            object_size = pixmap.size()

            if self.stack.currentIndex() == 0:
                self.scaled.setPixmap(pixmap)
            else:
                self.raw_label.setPixmap(pixmap)

        if self.stack.currentIndex() == 1:
            self.raw_label.resize(object_size)

        self.update()

        # If it doesn't fit, shorten the path rather than the details that are for comparing duplicates
        self.status.set_elided_text(
            preview.path,
            f" ({human_bytes(preview.file_size)} {extra}"
            f"- {object_size.width()},{object_size.height()}px "
            f"- {preview.modified}"
            f")",
        )


class ScaledLabel(QtWidgets.QLabel):
    """
    A version of ScaledLabel but supporting gifs/movies as well as images

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
        """Clear the label, and stop its movie"""
        super().clear()
        self.orig_pixmap = None
        if self.orig_movie:
            close_movie(self.orig_movie)
            self.orig_movie = None

    def minimumSizeHint(self):
        """Hijack minimumSizeHint"""
        # TODO: I don't know that this whole _minSize thing is required
        if self._minSize.isValid():
            return self._minSize
        return super().minimumSizeHint()

    def setPixmap(self, pixmap):
        """Hijack setPixmap"""
        # TODO: I'm not sure why I added this if statement but I think it can go after self.clear?
        if not pixmap:
            return

        self.clear()
        self.orig_pixmap = pixmap
        self._set_scaled_pixmap()

    def _set_scaled_pixmap(self):
        """Show orig_pixmap scaled to fit the label"""
        super().setPixmap(self.orig_pixmap.scaled(self.frameSize(), QtCore.Qt.AspectRatioMode.KeepAspectRatio))

    def resizeEvent(self, event):
        """Rescale the image to fit the new size (movies are scaled as they are painted)"""
        super().resizeEvent(event)
        if self.orig_pixmap and not self.movie():
            self._set_scaled_pixmap()

    def setMovie(self, movie):
        """Hijack setMovie"""
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

        self._movieSize, _ = movie_sizes(movie)
        width = self._movieSize.width()
        height = self._movieSize.height()

        minimum = min(width, height)
        maximum = max(width, height)
        ratio = maximum / minimum
        base = min(4, minimum)
        self._minSize = QtCore.QSize(base, round(base * ratio))
        if minimum == width:
            self._minSize.transpose()

        movie.jumpToFrame(0)
        self.updateGeometry()

    def paintEvent(self, event):
        """Paint the movie scaled to fit the label (images are scaled when they are set, and on resize)"""
        movie = self.movie()
        if not isinstance(movie, QtGui.QMovie) or not movie.isValid():
            # Setting the image again here would schedule another paint, repainting (and rescaling) nonstop
            super().paintEvent(event)
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
                movie.currentPixmap().scaled(cr.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio),
            )

        else:
            style.drawItemPixmap(
                qp, cr, alignment,
                movie.currentPixmap(),
            )


class ElidedLabel(QtWidgets.QLabel):
    """
    A one line label that shortens its text with "…" to fit its width, instead of growing to fit its text.

    When the text is shortened, hovering the label shows the whole text.

    A long file path in the image preview's status used to force the preview pane (and the window) wider.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Don't let the width of the text set a minimum width for the label, and the layouts it's in
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, self.sizePolicy().verticalPolicy())
        self._text = self.text()
        self._suffix = ''

    def setText(self, text: str):
        """Set the text, which is shortened in the middle if it doesn't fit"""
        self.set_elided_text(text)

    def set_elided_text(self, text: str, suffix: str = ''):
        """
        Set the text to shorten in the middle if it doesn't fit, followed by a suffix that's kept whole.

        The suffix is only shortened (at its end) if it doesn't fit even after all of the text has been shortened away.
        """
        self._text = text
        self._suffix = suffix
        self._show_elided_text()

    def resizeEvent(self, event):
        """Shorten the text for the new width"""
        super().resizeEvent(event)
        self._show_elided_text()

    def _show_elided_text(self):
        """Show the text, shortened to fit, with the whole text as a tooltip if it was shortened"""
        self.ensurePolished()  # So the font is the style sheet's (e.g. bold), which is wider
        # Measure in fractions of a pixel. Rounded to whole pixels, the suffix could measure narrower than it is,
        #   leaving the text a little too long and cutting off the end of the suffix
        metrics = QtGui.QFontMetricsF(self.font())
        width = self.contentsRect().width()
        suffix_width = metrics.horizontalAdvance(self._suffix)
        if suffix_width <= width:
            text = metrics.elidedText(self._text, QtCore.Qt.TextElideMode.ElideMiddle, width - suffix_width)
            text += self._suffix
        else:
            # Not even the suffix fits
            text = metrics.elidedText(self._suffix, QtCore.Qt.TextElideMode.ElideRight, width)

        super().setText(text)
        full_text = self._text + self._suffix
        self.setToolTip(full_text if text != full_text else '')
# endregion


# region Thumbnail tile panel
@cache
def get_overlay_icon(height, width):
    """Get the zip overlay icon"""
    return QtGui.QPixmap(ZIP_ICON_PATH).scaled(
        int(height), int(width),
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.FastTransformation,
    )


def _prompt_move_clear(parent, state) -> bool:
    """Ask the user to confirm clearing a complex state selection."""

    response = QtWidgets.QMessageBox.question(
        parent,
        "Clear?",
        f"Are you sure you want to clear the marking of {state.value}?",
        QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        QtWidgets.QMessageBox.StandardButton.No,
    )
    return response == QtWidgets.QMessageBox.StandardButton.Yes


class _ThumbnailJob(QtCore.QRunnable):
    """Loads thumbnails on a ThumbnailLoader's thread pool. A job's paths are all in the same zip, or it has one path"""

    def __init__(self, paths: list[ZipPath], thumb_size: int, loaded: QtCore.SignalInstance):
        super().__init__()
        self.paths = paths
        self.thumb_size = thumb_size
        self.loaded = loaded

    def run(self):
        """Load the thumbnails, handing each one to the GUI thread as it loads"""
        remaining = list(self.paths)
        try:
            with ExitStack() as open_zip:
                zf = None
                while remaining:
                    path = remaining[0]
                    image = QtGui.QImage()
                    # A file (or a file in a zip) that was changed or deleted outside of the app, like a zip whose
                    #   duplicates were cleaned out after it was scanned, gets an empty tile (like images Qt can't read)
                    with suppress(KeyError, OSError, BadZipFile):
                        if path.subpath and zf is None:
                            zf = open_zip.enter_context(ZipFile(path.path))
                        image = _load_thumbnail(path, self.thumb_size, zf)
                    self._report(remaining.pop(0), image)
        finally:
            # If loading failed unexpectedly, still report the rest (as empty) so they aren't left pending
            for path in remaining:
                self._report(path, QtGui.QImage())

    def _report(self, path: ZipPath, image: QtGui.QImage):
        """Hand a thumbnail to the GUI thread"""
        with suppress(RuntimeError):  # The loader was deleted, i.e. the app is closing
            self.loaded.emit(path, self.thumb_size, image)


class ThumbnailLoader(QtCore.QObject):
    """
    Loads thumbnails on a thread pool, and keeps the most recently used ones.

    Loading a page's thumbnails on the GUI thread froze it until every image was decoded (seconds, for large photos).
        This way the page shows straight away, each thumbnail appears as soon as it loads,
        and thumbnails that were shown recently appear instantly.

    Signals:
        thumbnailLoaded(path: ZipPath, thumb_size: int, pixmap: QPixmap): A thumbnail from `get` has loaded.
    """
    thumbnailLoaded = QtCore.Signal(ZipPath, int, QtGui.QPixmap)
    _imageLoaded = QtCore.Signal(ZipPath, int, QtGui.QImage)  # Emitted on the pool's threads, handled on the GUI's

    def __init__(self, parent=None, *, threads: int = 4, cache_size: int = 2000):
        super().__init__(parent)
        self._pool = QtCore.QThreadPool(self)
        self._pool.setMaxThreadCount(threads)
        self._cache_size = cache_size
        self._cache = OrderedDict()  # (path, thumb_size) -> QPixmap, least recently used first
        self._pending = set()  # (path, thumb_size) that are loading
        self._imageLoaded.connect(self._on_image_loaded)

        # Images in zips that were asked for, but not started yet: (zip path, thumb_size) -> paths.
        #   They're started together once the GUI is done asking (e.g. once a page is built), see _start_zip_jobs
        self._zip_requests = defaultdict(list)
        self._zip_timer = QtCore.QTimer(self, singleShot=True, interval=0)
        self._zip_timer.timeout.connect(self._start_zip_jobs)

        # The pool waits for every queued thumbnail when it's destroyed, so don't make closing wait for them too
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)

    def get(self, path: ZipPath, thumb_size: int) -> QtGui.QPixmap | None:
        """
        Get a thumbnail if it was loaded recently, otherwise start loading it.

        Returns:
            QtGui.QPixmap | None: The thumbnail, or None if it is loading. thumbnailLoaded is emitted when it's ready.
        """
        key = (path, thumb_size)
        pixmap = self._cache.get(key)
        if pixmap is not None:
            self._cache.move_to_end(key)
            return pixmap

        if key not in self._pending:
            self._pending.add(key)
            if path.subpath:
                self._zip_requests[path.path, thumb_size].append(path)
                self._zip_timer.start()
            else:
                self._pool.start(_ThumbnailJob([path], thumb_size, self._imageLoaded))

        return None

    def cancel(self):
        """Stop loading thumbnails that haven't started yet, e.g. because the page they were for has been left"""
        self._zip_timer.stop()
        self._zip_requests.clear()
        self._pool.clear()
        self._pending.clear()

    def wait(self):
        """Stop loading thumbnails that haven't started, and wait for the rest, so none of their files are open"""
        self.cancel()
        self._pool.waitForDone()

    def _start_zip_jobs(self):
        """
        Start loading the images in zips that were asked for.

        Opening a zip reads its whole list of files, in Python: ~9ms for a zip of 3000 files, holding the GIL, which
            slowed down the GUI too. So each zip is opened once per thread that loads images from it,
            rather than once per image.
        """
        for (_, thumb_size), paths in self._zip_requests.items():
            # Every thread gets some of the zip's images, so they still load in parallel (and the top rows first)
            jobs = min(self._pool.maxThreadCount(), len(paths))
            for i in range(jobs):
                self._pool.start(_ThumbnailJob(paths[i::jobs], thumb_size, self._imageLoaded))

        self._zip_requests.clear()

    def _on_image_loaded(self, path: ZipPath, thumb_size: int, image: QtGui.QImage):
        """Keep a thumbnail that finished loading, and pass it on as a QPixmap (which only the GUI thread can make)"""
        key = (path, thumb_size)
        self._pending.discard(key)
        pixmap = QtGui.QPixmap.fromImage(image)
        if not pixmap.isNull():
            # Images that couldn't be read aren't kept, so they're tried again the next time they're shown
            self._cache[key] = pixmap
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

        self.thumbnailLoaded.emit(path, thumb_size, pixmap)


class ThumbnailTile(QtWidgets.QFrame):
    """
    Clickable thumbnail tile that cycles between KEEP → DELETE → IGNORE.

    Attributes:
        path: Image path (opaque identifier for the caller).
        stateChanged(path: str, state: SelectionValue): Emitted on state updates.
        hovered(path: str): Emitted on cursor hovering over thumbnail tile.
        menuAction(action: str, path: ZipPath): Emitted when a context menu entry that acts on more than this tile, or
            needs more than this tile to act, is chosen. `action` names it, like "delete_group" or "move".
            MainWindow.on_tile_menu_action handles these.
    """
    # TODO: Convert state outline to overlay icon
    stateChanged = QtCore.Signal(ZipPath, SelectionValue)
    hovered = QtCore.Signal(ZipPath)
    menuAction = QtCore.Signal(str, ZipPath)

    def __init__(self, path: ZipPath, pixmap: QtGui.QPixmap | None = None, thumb_size: int = 32, parent=None):
        super().__init__(parent, frameShape=QtWidgets.QFrame.Shape.Box, lineWidth=2)
        self.setObjectName("ThumbnailTile")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

        self._path = path
        self._state = SelectionValues.KEEP
        self._thumb_size = thumb_size

        self._image = QtWidgets.QLabel(alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        if pixmap:
            self._image.setPixmap(pixmap)
        self._image.setFixedSize(thumb_size, thumb_size)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(NO_MARGIN)
        lay.setSpacing(0)
        lay.addWidget(self._image)

        if path.subpath:
            _overlay_icon = QtWidgets.QLabel(self._image)  # child of the tile so it floats over the image
            _overlay_icon.setObjectName("LockOverlay")
            _overlay_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            _overlay_icon.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, on=True)
            _overlay_icon.setFixedSize(thumb_size, thumb_size)  # small badge; adjust later if you want
            _overlay_icon.setPixmap(get_overlay_icon(thumb_size / 1.5, thumb_size / 1.5))

        # Files in zips are read-only, so they can't be deleted or moved, and the zip entries are only for them
        in_zip = self._path.is_zip
        self.context_menu = QtWidgets.QMenu(self)
        self._add_state_entry("Delete", SelectionValues.DELETE, enabled=not in_zip)
        self._add_action_entry("Delete group", "delete_group", enabled=not in_zip)
        self._add_action_entry("Delete column", "delete_column", enabled=not in_zip)
        self._add_action_entry("Delete folder", "delete_folder", enabled=not in_zip)
        self.context_menu.addSeparator()
        self._add_state_entry("Ignore", SelectionValues.IGNORE)
        self._add_action_entry("Ignore group", "ignore_group")
        self._add_action_entry("Ignore column", "ignore_column")
        self._add_action_entry("Ignore folder", "ignore_folder")
        self._add_action_entry("Ignore zip", "ignore_zip", enabled=in_zip)
        self.context_menu.addSeparator()
        self._add_action_entry("Move this file", "move", enabled=not in_zip)
        self.context_menu.addAction("Symlink this file").setEnabled(False)  # TODO:
        self.context_menu.addSeparator()
        self._add_state_entry("Un-select", SelectionValues.KEEP)
        self._add_action_entry("Un-select group", "unmark_group")
        self._add_action_entry("Un-select column", "unmark_column")
        self._add_action_entry("Un-select folder", "unmark_folder")
        self._add_action_entry("Un-select zip", "unmark_zip", enabled=in_zip)

        self._apply_state_style()

    def _add_state_entry(self, text: str, state: SelectionValue, *, enabled: bool = True):
        """Add a context menu entry that sets this tile's state"""
        entry = self.context_menu.addAction(text)
        entry.setEnabled(enabled)
        entry.triggered.connect(lambda _=False: self.change_state_clicked(state))

    def _add_action_entry(self, text: str, action: str, *, enabled: bool = True):
        """Add a context menu entry that's handled further up: it emits menuAction with `action`"""
        entry = self.context_menu.addAction(text)
        entry.setEnabled(enabled)
        entry.triggered.connect(lambda _=False: self.menuAction.emit(action, self._path))

    def set_pixmap(self, pixmap: QtGui.QPixmap):
        """Show the thumbnail image (a tile can be made before its thumbnail has loaded)"""
        self._image.setPixmap(pixmap)

    @property
    def path(self) -> ZipPath:
        """The internal file path"""
        return self._path

    @property
    def state(self) -> SelectionValue:
        """The internal state"""
        return self._state

    @state.setter
    def state(self, state: SelectionValue):
        """Set the tile selection state without cycling."""
        self.silent_set_state(state)
        self.stateChanged.emit(self._path, self._state)

    def silent_set_state(self, state: SelectionValue):
        """Set the state without invoking the stateChanged event"""
        if self._state == state:
            return

        locked = bool(self._path.subpath)
        if locked and state.state in [SelectionState.DELETE, SelectionState.MOVE]:
            raise ValueError(f"Cannot set a locked file to the {state.state.value} state!")

        self._state = state
        self._apply_state_style()

    def change_state_clicked(self, state: SelectionValue):
        """A change state button has been clicked in the context menu"""
        if self._state not in STATE_ORDER:
            if _prompt_move_clear(self, self._state.state):
                self.state = SelectionValues.KEEP
            return

        self.state = state

    def cycle_state(self):
        """Advance KEEP → DELETE → IGNORE → KEEP."""
        if self._state not in STATE_ORDER:
            if _prompt_move_clear(self, self._state.state):
                self.state = SelectionValues.KEEP
            return

        idx = STATE_ORDER.index(self._state)
        locked = bool(self._path.subpath)
        next_state = STATE_ORDER[(idx + 1) % len(STATE_ORDER)]
        # Cannot set zips to delete:
        if locked and next_state == SelectionValues.DELETE:
            next_state = STATE_ORDER[(idx + 2) % len(STATE_ORDER)]
        self.state = next_state

    def mousePressEvent(self, e: QtGui.QMouseEvent):
        """Cycle state on button press"""
        if e.button() == QtCore.Qt.MouseButton.LeftButton:
            self.cycle_state()
            e.accept()
        else:
            super().mousePressEvent(e)

    def enterEvent(self, e: QtGui.QEnterEvent):
        """Fire hover events"""
        self.hovered.emit(self._path)
        super().enterEvent(e)

    def _apply_state_style(self):
        """Update frame color"""
        color = STATE_COLORS[self._state.state]
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
            """,
        )

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent):
        """Handle opening the context menu"""
        self.context_menu.exec(event.globalPos())
        event.accept()


class DuplicateGroupRow(QtWidgets.QWidget):
    """
    A single row of thumbnails representing one duplicate group.

    Signals:
        tileStateChanged(path: ZipPath, state: SelectionValue)
        tileHovered(path: ZipPath)
        tileMenuAction(action: str, path: ZipPath): See ThumbnailTile.menuAction
    """
    tileStateChanged = QtCore.Signal(ZipPath, SelectionValue)
    tileHovered = QtCore.Signal(ZipPath)
    tileMenuAction = QtCore.Signal(str, ZipPath)

    def __init__(self, images: Sequence[ZipPath], thumb_size: int = 32, parent=None, *, thumbnails: ThumbnailLoader):
        super().__init__(parent)
        self._tiles: list[ThumbnailTile] = []
        self._thumb_size = thumb_size
        self._thumbnails = thumbnails
        self._thumbnails.thumbnailLoaded.connect(self._on_thumbnail_loaded)
        self.layout = QtWidgets.QHBoxLayout(self)
        self.layout.setContentsMargins(NO_MARGIN)
        self.layout.setSpacing(0)

        for path in images:
            self.add_tile(path)

        self.layout.addStretch(1)

    def __len__(self) -> int:
        """Define len operator for conveince"""
        return len(self._tiles)

    def tiles(self) -> list[ThumbnailTile]:
        """Fetch all the tiles (in a copy)"""
        return list(self._tiles)

    def add_tile(self, path: ZipPath):
        """Add a new tile to this duplicate group"""
        # If the thumbnail wasn't loaded recently, the tile starts empty and it's shown once it loads
        pixmap = self._thumbnails.get(path, self._thumb_size)
        tile = ThumbnailTile(path=path, pixmap=pixmap, thumb_size=self._thumb_size)
        tile.stateChanged.connect(self.tileStateChanged)
        tile.hovered.connect(self.tileHovered)
        tile.menuAction.connect(self.tileMenuAction)
        self._tiles.append(tile)
        self.layout.insertWidget(len(self._tiles) - 1, tile)

    def _on_thumbnail_loaded(self, path: ZipPath, thumb_size: int, pixmap: QtGui.QPixmap):
        """A thumbnail finished loading, so show it on this group's tiles for that image"""
        if thumb_size != self._thumb_size:
            return

        for tile in self._tiles:
            if tile.path == path:
                tile.set_pixmap(pixmap)


class ClickableLabel(QtWidgets.QLabel):
    """QLabel that emits a `clicked` signal on left mouse release."""
    clicked = QtCore.Signal()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        """Override the release event to emit to the clicked signal"""
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class DuplicateGroupList(QtWidgets.QWidget):
    """
    Scrollable list of duplicate groups. Each group renders as a row of thumbnails.

    Notes:
        - Clicking a thumbnail cycles KEEP → DELETE → IGNORE.
        - Borders/badges are colored by state.
    """

    groupTileStateChanged = QtCore.Signal(ZipPath, SelectionValue)  # path, state
    groupTileHovered = QtCore.Signal(ZipPath)
    groupTileMenuAction = QtCore.Signal(str, ZipPath)  # action, path. See ThumbnailTile.menuAction
    pageIndicatorClicked = QtCore.Signal()

    def __init__(self, parent=None, *, max_rows: int = 25, thumb_size: int = 64, **kwargs):
        super().__init__(parent, **kwargs)
        self._max_rows = max_rows
        self._thumb_size = thumb_size
        self._thumbnails = ThumbnailLoader(self)

        self._scroll = QtWidgets.QScrollArea(widgetResizable=True)
        self._container = QtWidgets.QWidget()
        self._vbox = QtWidgets.QVBoxLayout(self._container)
        self._vbox.setContentsMargins(NO_MARGIN)
        self._vbox.setSpacing(0)
        _tail_spacer = QtWidgets.QSpacerItem(
            0, 0, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self._vbox.addItem(_tail_spacer)
        self._scroll.setWidget(self._container)

        # Main layout
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(NO_MARGIN)
        outer.setSpacing(0)
        outer.addWidget(self._scroll)

        self._rows: list[DuplicateGroupRow] = []

        # Status bar
        _status = QtWidgets.QHBoxLayout()
        self.first_page = QtWidgets.QPushButton("<<")
        self.page_down = QtWidgets.QPushButton("<")
        self.page_indicator = ClickableLabel(alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        self.page_indicator.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.page_indicator.clicked.connect(self.pageIndicatorClicked)
        self.page_up = QtWidgets.QPushButton(">")
        self.last_page = QtWidgets.QPushButton(">>")
        _status.addWidget(self.first_page)
        _status.addWidget(self.page_down)
        _status.addWidget(self.page_indicator)
        _status.addWidget(self.page_up)
        _status.addWidget(self.last_page)
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
        self.clear()
        for group in groups[: self._max_rows]:
            self.add_group(group)

    def update_page_indicator(self, current_page: int, total_pages: int):
        """Update page indicator label"""
        self.page_indicator.setText(f"Page {current_page} of {total_pages}")

    def add_group(self, group: Sequence[ZipPath]) -> None:
        """Add a new match group, requiring a new row"""
        if len(self._rows) == self._max_rows:
            raise ValueError("Cannot add a new group to a fully filled group list!")

        row = DuplicateGroupRow(group, thumb_size=self._thumb_size, thumbnails=self._thumbnails)
        row.tileStateChanged.connect(self.groupTileStateChanged)
        row.tileHovered.connect(self.groupTileHovered)
        row.tileMenuAction.connect(self.groupTileMenuAction)
        tail_index = self._vbox.count() - 1
        self._vbox.insertWidget(tail_index, row)
        self._rows.append(row)

    def reset_states(self):
        """Set all tiles to KEEP."""
        for row in self._rows:
            for tile in row.tiles():
                tile.state = SelectionValues.KEEP

    def clear(self):
        """Reset this widget"""
        self._thumbnails.cancel()  # Any thumbnails still waiting to load were for these rows
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

    def release_files(self):
        """
        Make sure no file is open for a thumbnail: stop the thumbnails waiting to load, and wait for the ones loading.

        Call this before deleting or moving files: Windows won't delete or move a file that's open.
        """
        self._thumbnails.wait()

    def group_around(self, path: ZipPath) -> list[ZipPath]:
        """
        The other images in the row (group) that has `path`, nearest to it first.

        Returns:
            list[ZipPath]: The images, or an empty list if `path` isn't on this page.
        """
        for row in self._rows:
            paths = [tile.path for tile in row.tiles()]
            if path in paths:
                i = paths.index(path)
                # By distance, and the one to the right first when two are as near
                nearest = sorted(range(len(paths)), key=lambda j: (abs(j - i), j < i))
                return [paths[j] for j in nearest if j != i]

        return []

    def column_of(self, path: ZipPath) -> int | None:
        """
        Which column `path` is in: its place in its row (group), from 0.

        Returns:
            int | None: The column, or None if `path` isn't on this page.
        """
        for row in self._rows:
            for column, tile in enumerate(row.tiles()):
                if tile.path == path:
                    return column

        return None
# endregion


class _FolderIconProvider(QtWidgets.QFileIconProvider):
    """
    Icons for the folder tree, without working out a type for every file.

    As a folder loads, QFileSystemModel asks for the type of everything in it (like "JPEG image") on the GUI thread,
        including the files it then hides. That's a MIME lookup per file: System32's ~5000 files froze the GUI for ~1s.
        The tree doesn't show types, so any type will do.
    """

    def type(self, info: QtCore.QFileInfo) -> str:
        """
        A type that's quick to work out.

        Returns:
            str: "Folder" or "File". It can't be empty: QFileSystemModel only shows a new entry once its details
                differ from those of its placeholder, which has no type. So with empty types no folder would show up.
        """
        return "Folder" if info.isDir() else "File"


def _is_hidden(entry: os.DirEntry) -> bool:
    """Is this hidden, as QDir decides it? That's the hidden attribute on Windows, and a leading dot elsewhere"""
    if sys.platform == "win32":
        return bool(entry.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)

    if sys.platform == "darwin" and entry.stat().st_flags & stat.UF_HIDDEN:
        return True

    return entry.name.startswith(".")


def _has_shown_subfolder(path: str, *, include_hidden: bool) -> bool:
    """
    Does this folder have a subfolder that the folder tree would show? This stops looking at the first one.

    Shortcuts (.lnk files) don't count. Qt shows shortcuts to folders as folders, but pixmatch can't scan them,
        and resolving them is slow (seconds for a desktop full of them).

    Returns:
        bool: Whether it has one. False if the folder can't be read, like QDir.
    """
    with suppress(OSError), os.scandir(path) as entries:
        for entry in entries:
            with suppress(OSError):
                if entry.is_dir() and (include_hidden or not _is_hidden(entry)):
                    return True

    return False


class DirFileSystemModel(QtWidgets.QFileSystemModel):
    """
    Custom FileSystemModel, which only shows an expand arrow for folders that have subfolders.

    QFileSystemModel gives every folder an arrow, and once a folder with no subfolders is opened it still shows one.
    The tree asks again about every folder it shows each time it lays itself out, so looking in each folder on the GUI
        thread froze it in big folders. Instead folders are looked in on worker threads, and get their arrow then.
    Once Qt has loaded a folder's contents (opening it does that), those are used instead, as Qt keeps them up to date.
        Qt doesn't watch folders that haven't been opened though, see recheck_children for those.

    Folders are kept track of by their indexes' internalId, which points at Qt's record of the folder: working out a
        folder's path took most of the time of hasChildren, which is called hundreds of thousands of times while a big
        folder loads (and finding a folder's index from its path took ~1ms). Qt reuses those ids once a folder is
        removed, so removed folders are forgotten before then.
    Folders waiting to be looked in keep their row rather than a QPersistentModelIndex: Qt updates those each time it
        sorts a folder (which it does as a folder loads), and with thousands of them that froze it for seconds.
    """

    # Folders are looked in on more than one thread, so one that's slow to read (like an offline network drive)
    #   doesn't hold up the rest
    CHECK_THREADS = 2

    _checked = QtCore.Signal(list)  # [(id, path, generation, has subfolders)], emitted on the worker threads

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._icon_provider = _FolderIconProvider()  # Kept here, as the model doesn't keep it alive
        self.setIconProvider(self._icon_provider)

        self._loaded = set()  # Folders whose contents Qt has finished loading
        self._has_subfolders = {}  # Folders that have been looked in -> whether they have subfolders
        self._stale = {}  # Answers from before the filter changed, shown until their folders are looked in again
        self._pending = {}  # Folders waiting to be looked in -> (their path, their folder's id, their row)
        self._parents = {}  # Folders of folders that have been asked about -> their index (0 is the drives' folder)
        self._rows = {}  # Folders -> {the id of each folder in it: its row}, when looked up (until rows change)
        self._generation = 0  # Changes with the filter, so answers found with the old filter are thrown away
        self._requests = queue.SimpleQueue()  # (id, path, generation, include hidden folders)

        self.directoryLoaded.connect(self._on_directory_loaded)
        self.rowsAboutToBeRemoved.connect(self._forget_rows)
        self.modelAboutToBeReset.connect(self._forget_all)
        for rows_changed in (self.rowsInserted, self.rowsRemoved, self.layoutChanged):
            rows_changed.connect(self._rows.clear)
        self._checked.connect(self._on_checked)
        for _ in range(self.CHECK_THREADS):
            threading.Thread(target=self._check_folders, daemon=True).start()

    def setFilter(self, filters: QtCore.QDir.Filter):
        """Set the filter. Whether a folder has subfolders can change with it (hidden ones), so look again"""
        super().setFilter(filters)
        self._generation += 1
        self._stale.update(self._has_subfolders)
        self._has_subfolders.clear()

    def hasChildren(self, /, parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = ...):
        """Return true if the folder has subfolders. Until it has been looked in, return false"""
        if not parent.isValid() or parent.column() > 0:
            return super().hasChildren(parent)

        folder = parent.internalId()
        if folder in self._loaded:
            return self.rowCount(parent) > 0

        has_subfolders = self._has_subfolders.get(folder)
        if has_subfolders is None:
            self._request(parent)
            return self._stale.get(folder, False)

        return has_subfolders

    def recheck_children(self, parent: QtCore.QModelIndex):
        """
        Look again in the folders in a folder, to update their arrows. They keep the arrows they have until then.

        This is for the tree's expanded signal: Qt doesn't watch folders that haven't been opened, so this is how their
            arrows catch up with subfolders made or deleted outside of the app.
        """
        for row in range(self.rowCount(parent)):
            index = self.index(row, 0, parent)
            if index.internalId() in self._has_subfolders and index.internalId() not in self._loaded:
                self._request(index)

    def _request(self, index: QtCore.QModelIndex):
        """Have a folder looked in, unless it's already waiting to be"""
        folder = index.internalId()
        if folder in self._pending:
            return

        parent = index.parent()
        parent_id = parent.internalId()  # 0 for the drives, whose folder is the (invalid) root index
        if parent_id not in self._parents:
            self._parents[parent_id] = QtCore.QPersistentModelIndex(parent)
        path = self.filePath(index)
        self._pending[folder] = (path, parent_id, index.row())
        self._requests.put((folder, path, self._generation, bool(self.filter() & QtCore.QDir.Filter.Hidden)))

    def _check_folders(self):
        """Look in folders as they're asked for (on a worker thread), and hand back what was found in batches"""
        while True:
            request = self._requests.get()
            found = []
            batch_end = time.monotonic() + 0.02  # Up to 20ms of answers go to the GUI thread together
            while request is not None:
                folder, path, generation, include_hidden = request
                found.append((folder, path, generation, _has_shown_subfolder(path, include_hidden=include_hidden)))
                request = None
                if time.monotonic() < batch_end:
                    with suppress(queue.Empty):
                        request = self._requests.get_nowait()

            with suppress(RuntimeError):  # The model was deleted, i.e. the app is closing
                self._checked.emit(found)

    def _on_checked(self, found: list):
        """Keep what the worker threads found, and update the arrows that changed"""
        changed = defaultdict(list)  # Folders' ids -> the (id, row) of the folders in them to update
        for folder, path, generation, has_subfolders in found:
            pending = self._pending.get(folder)
            if pending is None or pending[0] != path:
                continue  # The folder was removed while it was looked in (and its id may be another folder's now)

            del self._pending[folder]
            _, parent_id, row = pending
            if generation == self._generation:
                had_subfolders = self._has_subfolders.get(folder, self._stale.pop(folder, False))
                self._has_subfolders[folder] = has_subfolders
                if has_subfolders == had_subfolders:
                    continue
            # Otherwise the filter changed while it was looked in. Updating it has the tree ask again, which looks again

            changed[parent_id].append((folder, row))

        for parent_id, folders in changed.items():
            self._update_rows(parent_id, folders)

    def _update_rows(self, parent_id: int, folders: list):
        """Have the tree ask again whether some folders in a folder have subfolders"""
        if parent_id and parent_id not in self._loaded:
            # The tree asks about every row again each time a loading folder gets more, and once it's loaded
            #   (see _on_directory_loaded). Its rows also move as Qt sorts them, so they couldn't be updated one by one
            return

        parent = self._parents.get(parent_id)
        if parent is None or (parent_id and not parent.isValid()):
            return  # Their folder was removed

        parent = self.index(parent.row(), 0, parent.parent()) if parent_id else QtCore.QModelIndex()
        for folder, row in folders:
            index = self.index(row, 0, parent)
            if index.internalId() != folder:
                # It moved since it was asked about (like when Qt sorted the folder), so look its row up
                rows = self._rows.get(parent_id)
                if rows is None:
                    rows = {self.index(r, 0, parent).internalId(): r for r in range(self.rowCount(parent))}
                    self._rows[parent_id] = rows
                if folder not in rows:
                    continue
                index = self.index(rows[folder], 0, parent)

            self.dataChanged.emit(index, index)

    def _on_directory_loaded(self, path: str):
        """
        Qt has loaded a folder's contents, so have the tree check the folder's arrow against them.

        The arrows of the folders in it are updated too, for what was found about them while it loaded.
        """
        index = self.index(path)
        if index.isValid():
            self._loaded.add(index.internalId())
            self.dataChanged.emit(index, index)
            rows = self.rowCount(index)
            if rows:
                self.dataChanged.emit(self.index(0, 0, index), self.index(rows - 1, 0, index))

    def _forget_rows(self, parent: QtCore.QModelIndex, first: int, last: int):
        """Forget folders that are about to be removed (and the folders in them), before Qt reuses their ids"""
        indexes = [self.index(row, 0, parent) for row in range(first, last + 1)]
        while indexes:
            index = indexes.pop()
            folder = index.internalId()
            self._has_subfolders.pop(folder, None)
            self._stale.pop(folder, None)
            self._pending.pop(folder, None)
            self._parents.pop(folder, None)
            self._loaded.discard(folder)
            indexes.extend(self.index(row, 0, index) for row in range(self.rowCount(index)))

    def _forget_all(self):
        """Forget every folder, as the model is being reset"""
        self._loaded.clear()
        self._has_subfolders.clear()
        self._stale.clear()
        self._pending.clear()
        self._parents.clear()
