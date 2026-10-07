import hashlib
import logging
import os
import time

from bisect import bisect_left, insort
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial, wraps
from itertools import combinations
from multiprocessing import Pool
from pathlib import Path
from queue import Queue
from threading import Event, RLock, Semaphore
from typing import Callable, ClassVar, Iterable, Sequence
from zipfile import BadZipFile, ZipFile

import numpy as np

from PIL import Image, ImageFile, UnidentifiedImageError

from pixmatch import phash

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZipPath:
    """
    A general object describing a Path.

    All paths in pixmatch will be one of these. `subpath` will be empty for non-zip file paths.

    Attributes:
        path (str): The path to the file.
        subpath (str): The subpath in the zip if `path` is for a zip.
    """
    # TODO: At some point convert this to Path.
    #   When I tried that last it introduced problems with inter-process communication
    path: str
    subpath: str = ''

    def __str__(self) -> str:
        """Show a file in a zip as if the zip were a folder, like Windows Explorer does"""
        if not self.subpath:
            return self.path

        # Zips separate folders with "/" on every OS
        return self.path + os.sep + self.subpath.replace('/', os.sep)

    @property
    def path_obj(self) -> Path:
        """The path as a Path object"""
        return Path(self.path)

    @property
    def is_gif(self) -> bool:
        """Is this a path to an animated image?"""
        movie_extensions = {'.gif', '.webp'}
        suffix = Path(self.subpath or self.path).suffix.lower()
        return suffix in movie_extensions

    @property
    def is_zip(self) -> bool:
        """Does this point to a file located in a zip?"""
        return bool(self.subpath)

    def absolute(self):
        """Get the absolute version of this ZipPath"""
        return ZipPath(str(self.path_obj.expanduser().resolve()), self.subpath)


def _is_under(folder_abs: str, target: str | Path) -> bool:
    """Is target (a file or folder) inside the folder folder_abs, or that folder itself? Links in both are followed"""
    try:
        Path(target).expanduser().resolve().relative_to(Path(folder_abs).expanduser().resolve())
    except ValueError:
        return False

    return True


# The most bits two images' hashes can differ by to match, for each strength from 0 (loose) to 10 (strict).
#   Every hash has exactly 32 of its 64 bits set, so two hashes always differ by an even number of bits.
#   From testing: re-saves, resizes, format changes, rotations and small brightness or color changes land within
#   2-4 bits, and trimming 1% off each side within 6. Unrelated images were more than 6 apart (and usually 20+).
#   Past 8 bits, HammingIndex gets much slower (~16ms per image with a million or more images loaded).
STRENGTH_RADIUS = (8, 8, 8, 6, 6, 4, 4, 2, 2, 0, 0)

# Decoding JPEGs at a reduced size (see calculate_hashes' draft) can move a hash by up to 2 bits. That is harmless
#   when matches can differ by at least this many bits, but loses matches at stricter strengths.
DRAFT_RADIUS = 4

# The most files handed to the pool at once for each of its processes, counting the ones whose results are waiting to be
#   added. That's plenty to keep every process busy. The walk finds files far faster than they can be hashed, and
#   handing each one over as soon as it was found took ~2.8KB per file until it was hashed (~2.7GB for a million).
#   The files waiting their turn are kept as just their paths instead, which took ~250 bytes per file
TASKS_PER_PROCESS = 8

# Formats whose extra frames are an animation. Other formats can have more than one frame too, but their first frame is
#   the image: like a TIFF's other pages, or the preview that many cameras add to their JPEGs (which open as MPO)
ANIMATION_FORMATS = {"GIF", "PNG", "WEBP"}


def _is_animation(im: Image.Image) -> bool:
    """Is this image an animation? This goes by what's in the file, not its extension"""
    if im.format not in ANIMATION_FORMATS:
        return False

    try:
        return im.is_animated
    except Exception:  # ruff: ignore[blind-except]
        # Counting a damaged file's frames can fail (e.g. Pillow raises IndexError for a GIF that was cut off in its
        #   second frame's header), but its first frame can still be hashed, like a still image's
        return False


def calculate_hashes(f, *, exact_match=False, draft=False) -> tuple[int | str, set[int]]:
    """
    Calculate hashes for a given file.

    Args:
        f (IO or str or Path): Either a file path to process, or a in-memory BytesIO object ready for reading.
        exact_match (bool): Use exact SHA256 hahes?
            If true, strength must be 10.
            If false, perceptual hashes will be used, even with high strength.
        draft (bool): Decode JPEGs at a reduced size? Much faster, but can change the hash by a couple bits.

    Returns:
        tuple[int | str, set]: The first element is the primary hash,
            the second element are any secondary hashes representing rotations, flips, etc...
            Perceptual hashes are 64-bit ints, and the SHA256 hash is a hex string (with no secondary hashes).
    """
    if exact_match:
        hasher = hashlib.sha256()
        block_size = 65536
        with (open(f, "rb") if isinstance(f, (str, Path)) else f) as file:  # ruff: ignore[builtin-open]
            for block in iter(lambda: file.read(block_size), b""):
                hasher.update(block)
        return hasher.hexdigest(), set()

    with Image.open(f) as im:
        animated = _is_animation(im)
        if draft:
            # JPEGs can be decoded at 1/2 to 1/8 of their size (and straight to grayscale), which is several times
            #   faster than decoding all of a large photo just to shrink it. This does nothing for other formats.
            im.draft("L", (phash.HASH_IMG_SIZE * 4, phash.HASH_IMG_SIZE * 4))
        orientation = im.getexif().get(phash.EXIF_ORIENTATION_TAG, 1)
        px = phash.grayscale_pixels(im, phash.HASH_IMG_SIZE)
        if animated:
            # Some animations start with a blank frame (nothing, or only a single color). Its hash would be noise
            #   that happens to be shared by other blank frames, so use the first frame that has something in it.
            while phash.is_flat(px):
                try:
                    im.seek(im.tell() + 1)
                except EOFError:  # ruff: ignore[try-except-in-loop]
                    break
                else:
                    px = phash.grayscale_pixels(im, phash.HASH_IMG_SIZE)

    # Hash the image the way it is displayed, not the way it is stored
    if orientation in phash.EXIF_ORIENTATIONS:
        px = phash.EXIF_ORIENTATIONS[orientation](px)

    if phash.is_flat(px):
        return phash.FLAT_HASH, set()

    # All the rotations and mirrors are made from the one small resized array, which is far cheaper than
    #   transforming the full size image. For animations we'll look for mirrored versions but thats it
    variants = (px, np.fliplr(px)) if animated else tuple(phash.orientations(px))
    initial_hash, *extras = (phash.phash(variant) for variant in variants)
    return initial_hash, set(extras)


def _init_worker(load_truncated_images: bool):  # ruff: ignore[boolean-type-hint-positional-argument]
    """
    Set up a process of the pool that hashes images.

    Pillow's settings belong to each process, and the pool's processes don't get the settings of the one that made them
        (on Windows they start from scratch). So a setting like allowing damaged images is passed on here.
    """
    ImageFile.LOAD_TRUNCATED_IMAGES = load_truncated_images


def thread_error_handler(func):
    """An error handler for the thread to return information about where the error occurred"""

    @wraps(func)
    def wrapper(path, *args, **kwargs):  # ruff: ignore[missing-return-type-private-function]
        try:
            return func(path, *args, **kwargs)
        except Exception as e:
            e.input_path = path
            raise

    return wrapper


@thread_error_handler
def _process_image(
        path: str | Path,
        supported_extensions: set | None = None,
        *,
        exact_match: bool = False,
        draft: bool = False,
) -> tuple[Path, tuple | dict[str, tuple]]:
    """Get the hashes for a given path. Is multiprocessing compatible"""
    path = Path(path)
    if path.suffix.lower() != '.zip':
        return path, calculate_hashes(path, exact_match=exact_match, draft=draft)

    if not supported_extensions:
        supported_extensions = ImageMatcher.SUPPORTED_EXTS

    results = {}
    with ZipFile(path) as zf:
        for f in zf.filelist:
            f_ext = Path(f.filename).suffix.lower()
            if f_ext not in supported_extensions:
                continue

            if f_ext == '.zip':
                logger.warning('Have not implemented nested zip support yet! Input file: %s (%s)', path, f)
                continue

            try:
                with zf.open(f) as zipped_file:
                    results[f.filename] = calculate_hashes(zipped_file, exact_match=exact_match, draft=draft)
            except BadZipFile as e:
                logger.warning("Could not read %s in %s due to %s", f.filename, path, e)
            except UnidentifiedImageError:
                logger.warning("Could not identify image %s in %s", f.filename, path)
            except Exception as e:  # ruff: ignore[blind-except]
                # One image that can't be hashed (too large, encrypted, ...) shouldn't lose every other image in the zip
                logger.warning("Could not process %s in %s due to %s: %s", f.filename, path, type(e).__name__, e)

    return path, results


@dataclass
class ImageMatch:
    """A match data structure containing the matches and where this match lies in the match list"""
    # Its place in ImageMatcher.matches, or None if it isn't a match: it has no duplicates (yet), or a group filter
    #   drops it (see ImageMatcher.set_group_filters)
    match_i: int | None = field(default=None)
    matches: list[ZipPath] = field(default_factory=list)

    def snapshot(self) -> "ImageMatch":
        """
        A copy of this group as it is now, for events.

        Events are read later on another thread, and the group keeps changing until then. Without a copy, the GUI
            would build a new group's row from images whose own NewMatch events are still queued, and show them twice.

        Returns:
            ImageMatch: The copy, which won't change as images are added to or removed from this group.
        """
        return ImageMatch(self.match_i, list(self.matches))


# region Events
@dataclass(frozen=True)
class NewGroup:
    """A new group event"""
    group: "ImageMatch"
    seq: int  # The event's number. They're numbered from 1 in the order they're sent, see ImageMatcher.events_sent


@dataclass(frozen=True)
class NewMatch:
    """A new match event"""
    group: "ImageMatch"
    path: ZipPath
    seq: int  # The event's number, like NewGroup's


@dataclass(frozen=True)
class Finished:
    """A finished event"""


MatcherEvent = NewGroup | NewMatch | Finished
# endregion


# region Group filters
# A group filter is given the images in a group of duplicates, and returns whether to keep the group as a match (True)
#   or drop it from ImageMatcher.matches (False). See ImageMatcher.set_group_filters
GroupFilter = Callable[[Sequence[ZipPath]], bool]
# endregion


class HammingIndex:
    """
    Finds the stored hashes within some number of differing bits (radius) of a hash, without checking all of them.

    This is multi-index hashing: every hash is split into chunks, and each chunk gets its own lookup table.
    If two hashes are within the radius, then (by the pigeonhole principle) at least one of their chunks differs by
    no more than radius // CHUNKS bits. So looking up every value that close to each of a hash's chunks finds every
    stored hash that could be in range, and only those candidates get compared in full.

    Args:
        radius (int): The most bits a stored hash can differ by to be found.
    """
    BITS = phash.HASH_SIZE * phash.HASH_SIZE
    CHUNKS = 3  # 21-22 bit chunks keep the tables' buckets small, even with a couple million hashes stored

    def __init__(self, radius: int):
        self.radius = radius
        widths = [self.BITS // self.CHUNKS + (i < self.BITS % self.CHUNKS) for i in range(self.CHUNKS)]

        # For each chunk: where it is in the hash, and every change to it of at most radius // CHUNKS bits
        self._chunks = []
        shift = 0
        for width in widths:
            flips = [sum(1 << bit for bit in bits)
                     for n in range(radius // self.CHUNKS + 1) for bits in combinations(range(width), n)]
            self._chunks.append((shift, (1 << width) - 1, flips))
            shift += width

        # Chunk value -> the stored hash with that chunk value, or a list of them if there are several.
        #   Most chunk values only ever belong to one hash, so skipping the list for those saves a lot of memory.
        self._tables = [{} for _ in widths]

    def add(self, hash_: int):
        """Store a hash"""
        for (shift, mask, _), table in zip(self._chunks, self._tables, strict=True):
            key = hash_ >> shift & mask
            stored = table.get(key)
            if stored is None:
                table[key] = hash_
            elif isinstance(stored, list):
                stored.append(hash_)
            else:
                table[key] = [stored, hash_]

    def remove(self, hash_: int):
        """Remove a stored hash"""
        for (shift, mask, _), table in zip(self._chunks, self._tables, strict=True):
            key = hash_ >> shift & mask
            stored = table[key]
            if stored == hash_:
                del table[key]
                continue

            stored.remove(hash_)
            if len(stored) == 1:
                table[key] = stored[0]

    def find(self, hash_: int) -> dict[int, int]:
        """Find the stored hashes within the radius of a hash, as a dict of stored hash -> bits different"""
        found = {}
        for (shift, mask, flips), table in zip(self._chunks, self._tables, strict=True):
            key = hash_ >> shift & mask
            for flip in flips:
                stored = table.get(key ^ flip)
                if stored is None:
                    continue

                for candidate in (stored if isinstance(stored, list) else (stored, )):
                    if candidate not in found:
                        distance = (candidate ^ hash_).bit_count()
                        if distance <= self.radius:
                            found[candidate] = distance

        return found


class ImageMatcher:
    """
    An image matching SDK

    Images are hashed in other processes, which use this process's setting for PIL.ImageFile.LOAD_TRUNCATED_IMAGES.
        So set that to True to hash damaged images too (the app does), rather than skipping them.

    While `run` is running, images are added on another thread. To change the matcher's data or read through it
        (like `matches`) from any other thread meanwhile, do it in a `with matcher.paused():` block.

    Args:
        strength (int): The 0-10 strength to use for matching. Defaults to 5.
            STRENGTH_RADIUS has how many bits two images' hashes can differ by at each strength.
        exact_match (bool): Should use SHA-256 hashes? If False, the default, will use perceptual hashes.
            If True, strength must be 10.
        processes (int): The number of processes to use. Defaults to None, for one per CPU.
        extensions (set): The extensions to process. Optional.
        group_filters (Iterable[GroupFilter]): Only the groups of duplicates that all of these keep are matches (are in
            `matches`), see set_group_filters. Defaults to none, so every group of duplicates is a match.
    """
    SUPPORTED_EXTS: ClassVar = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".zip"}

    def __init__(self, strength: int = 5, processes: int | None = None, extensions: set | None = None,
                 *, exact_match: bool = False, group_filters: Iterable[GroupFilter] = ()):
        if not (0 <= strength <= 10):
            raise ValueError("Strength must be between 0 and 10!")

        self.extensions = extensions or self.SUPPORTED_EXTS

        self.strength = strength
        self.radius = STRENGTH_RADIUS[strength]
        self.exact_match = exact_match
        self.processes = processes
        self._group_filters = tuple(group_filters)  # See set_group_filters

        self.found_images = 0
        self.processed_images = 0
        self.duplicate_images = 0  # How many images the groups in `matches` have
        self.matches = []

        # Both ends of these queues are in this process (the pool runs its callbacks on a thread here, not in the
        #   workers), so they don't need to be multiprocessing queues
        self.events = Queue()  # Events to go to higher level users
        # The seq of the last NewGroup or NewMatch event sent. Data read in a paused() block has the changes of every
        #   event up to this one, so a user can tell which events (maybe still queued) it has already seen
        self.events_sent = 0
        self._new_paths = Queue()  # Inbound queue for new paths that are added while processing is running
        self._removed_paths = set()  # Paths that have been removed from processing after processing has been started
        self._ignored_files = set()  # Files which have been ignored and should be skipped from processing if re-ran
        self._processed_zips = {}  # Zips that have been successfully processed
        self._hashes = defaultdict(ImageMatch)  # Hash -> Paths
        self._reverse_hashes = {}  # Path -> Hash

        # Finds the hashes of _hashes that are close to an image's hashes. Not needed if they must be identical
        self._index = HammingIndex(self.radius) if self.radius and not exact_match else None

        # Pausing and finished signaling...
        self._not_paused = Event()
        self._not_paused.set()
        self._finished = Event()
        self._finished.set()

        # Held while an image is added, and by paused(), so the matcher's data is never changed by two threads at once,
        #   or changed while another thread reads through it
        self._lock = RLock()
        self._paused_depth = 0  # How many paused() blocks are open. Only changed while holding _lock
        # The match_i of every group taken out of `matches` in the open paused() block, sorted. The groups after them
        #   are renumbered when the block ends, see remove
        self._unmatched = []

        # How many more files can be handed to the pool, see TASKS_PER_PROCESS. Each run makes its own for its processes
        self._free_slots = None

    @property
    def left_to_process(self):
        """Files that are left to process"""
        return self.found_images - self.processed_images

    def add_path(self, path: str | Path):
        """Add a path for processing"""
        path = str(Path(path).expanduser().resolve())
        self._removed_paths.discard(path)
        self._new_paths.put(path)

    def remove_path(self, folder: str | Path):
        """
        Mark a folder to be skipped going forward, and remove already-indexed files
        that live under it. Pauses briefly if not already paused to keep state sane.

        Files under it that a running scan found, but hasn't handed to the pool yet, are skipped (see _send_files).
            The few that were handed over already are still hashed, and then dropped (see _add_result).
        """
        folder = str(Path(folder).expanduser().resolve())
        with self.paused():
            self._removed_paths.add(folder)

            # Remove anything we've already seen under that folder
            # (iterate over a copy because remove() mutates structures)
            to_remove = [p for p in self._reverse_hashes if _is_under(folder, p.path)]
            for p in to_remove:
                self.remove(p)

            to_remove_zips = [p for p in self._processed_zips if _is_under(folder, p)]
            for p in to_remove_zips:
                self._processed_zips.pop(p)

    def _is_removed(self, path: str | Path) -> bool:
        """Is this file or folder in a folder that was removed from processing (see remove_path)?"""
        # Copied first: another thread can add or remove a folder at any time, and a set that changes while it's being
        #   iterated raises
        return any(_is_under(folder, path) for folder in tuple(self._removed_paths))

    @contextmanager
    def paused(self):
        """
        Pause processing while the matcher's data is changed or read through, then resume (if it wasn't paused before).

        Pausing on its own only stops images from being added from then on: one that is being added carries on.
            So this also waits for that one, and keeps the data from changing until the `with` block ends.
            Blocks can be nested.
        Groups that stop being matches (see remove) are taken out of `matches` straight away, but the groups after them
            are only renumbered once the outermost block ends. So remove many files in one block: renumbering after each
            one took ~15ms per file with 100k groups after it.
        """
        was_paused = self.conditional_pause()
        try:
            with self._lock:
                self._paused_depth += 1
                try:
                    yield
                finally:
                    self._paused_depth -= 1
                    if not self._paused_depth:
                        self._renumber_matches()
        finally:
            self.conditional_resume(was_paused)

    def conditional_pause(self) -> bool:
        """Pause if not paused and return if was paused"""
        _conditional_pause = self.is_paused()
        if not _conditional_pause:
            logger.debug('Performing conditional pause')
            self.pause()

        return _conditional_pause

    def conditional_resume(self, was_paused: bool):  # ruff: ignore[boolean-type-hint-positional-argument]
        """Resume if not paused previous (from call to `conditional_pause`)"""
        if not was_paused and not self.is_finished():
            logger.debug('Performing conditional resume')
            self.resume()

    def pause(self):
        """Pause processing"""
        logger.debug('Performing pause')
        self._not_paused.clear()

    def is_paused(self):
        """Is processing paused"""
        return not self._not_paused.is_set()

    def finish(self):
        """Finish processing"""
        logger.debug('Performing finished')
        self._finished.set()
        # Wake up anything waiting for a pause to end, so it sees that processing is finished instead of waiting forever
        self._not_paused.set()

    def is_finished(self):
        """Is processing finished"""
        return self._finished.is_set()

    def resume(self):
        """Resume processing"""
        logger.debug('Performing resume')
        self._not_paused.set()

    def running(self):
        """Currently running and loading hashes?"""
        return not self.is_paused() and (not self.is_finished() or self.left_to_process)

    def remove(self, path):
        """Remove a loaded path completely from the image matching system. Will not delete a file."""
        # Pause things while we remove things...
        logger.info('Removing %s from %s', path, self.__class__.__name__)
        with self.paused():
            hash_ = self._reverse_hashes.pop(path)
            group = self._hashes[hash_]
            group.matches.remove(path)
            if group.match_i is not None:
                self.duplicate_images -= 1

            if not group.matches:
                logger.debug('Removing empty match group')
                del self._hashes[hash_]
                if self._index is not None:
                    self._index.remove(hash_)

            else:
                # With one image left it isn't a match anymore, and the group filters may decide differently now
                self._update_match(group)

            self.processed_images -= 1
            self.found_images -= 1

    def ignore(self, path):
        """Remove a path from the image matching service"""
        try:
            self.remove(path)
        except KeyError:
            logger.info("Failed to remove file %s, probably due to it being deleted already", path)

        if path.path_obj.suffix.lower() != '.zip':
            self._ignored_files.add(path.path)

    def rename(self, old_path: ZipPath, new_path: ZipPath):
        """Update a loaded path after its file was moved on disk. Will not move a file."""
        if old_path == new_path:
            return

        logger.info('Renaming %s to %s in %s', old_path, new_path, self.__class__.__name__)
        with self.paused():
            if new_path in self._reverse_hashes:
                # The file that was at new_path has been replaced by this one
                self.remove(new_path)

            hash_ = self._reverse_hashes.pop(old_path)
            self._reverse_hashes[new_path] = hash_

            # Replace it in place, so the file keeps its position (column) in its group
            matches = self._hashes[hash_].matches
            matches[matches.index(old_path)] = new_path
            # Where the file is now can change what the group filters decide about its group
            self._update_match(self._hashes[hash_])

    def set_group_filters(self, group_filters: Iterable[GroupFilter]):
        """
        Choose which groups of duplicates are matches (are in `matches`): only the ones that all of these keep.

        A group that a filter drops still gets its images, but it isn't in `matches`, it has no match_i, its images
            aren't counted in duplicate_images, and no events are sent about it. If an image added to it makes the
            filters keep it, it becomes a match at the end of `matches` with a NewGroup event, like a new group.
        A match is checked again when its images are removed or moved, but not as images are added to it: taking it out
            of `matches` mid-scan would renumber the groups after it while events about them are on their way.
            So a filter shouldn't drop a group because an image was added to it.

        Changing the filters sends no events, so read `matches` again afterwards. The groups that stay matches keep
            their order, and the ones that become matches go after them, in the order their first images were found.

        Args:
            group_filters (Iterable[GroupFilter]): The filters. Each is given a group's images (which it mustn't
                change) and returns whether to keep the group. They're called as images are added, so make them quick.
        """
        with self.paused():
            self._group_filters = tuple(group_filters)

            # Every group is checked, so `matches` is made again in one go. Taking the groups out one at a time (like
            #   remove does) moves every group after each one: 0.7s to take out 50k of 100k groups
            kept = []
            for group in self.matches:
                if self._counts_as_match(group):
                    kept.append(group)
                else:
                    group.match_i = None
                    self.duplicate_images -= len(group.matches)

            # The groups the old filters dropped that these keep (a dict keeps the order its keys were added in)
            added = [group for group in self._hashes.values() if group.match_i is None and self._counts_as_match(group)]
            self.duplicate_images += sum(len(group.matches) for group in added)
            self.matches[:] = kept + added

            # Every group is renumbered here, so the paused() block this is in (if any) has nothing left to renumber
            self._unmatched.clear()
            self.refresh_match_indexes()

    def refresh_match_indexes(self, start=0):
        """Update the match_i value for all the matches passed a certain point"""
        for match_i, match in enumerate(self.matches[start:], start=start):
            match.match_i = match_i

    def _renumber_matches(self):
        """Renumber the groups after the first one taken out of `matches` in the paused() block that just ended"""
        if self._unmatched:
            # The groups before the first one taken out kept their places, so their numbers are still right
            self.refresh_match_indexes(self._unmatched[0])
            self._unmatched.clear()

    def _counts_as_match(self, group: ImageMatch) -> bool:
        """Should this group be a match (be in `matches`)? It needs duplicates, and every group filter has to keep it"""
        return len(group.matches) >= 2 and all(keep(group.matches) for keep in self._group_filters)

    def _match(self, group: ImageMatch):
        """Make a group a match, at the end of `matches`"""
        # Numbered like the groups before it: in a paused() block, that still counts the groups taken out of `matches`
        #   until the block ends and renumbers them (see _unmatch)
        group.match_i = len(self.matches) + len(self._unmatched)
        self.matches.append(group)
        self.duplicate_images += len(group.matches)

    def _unmatch(self, group: ImageMatch):
        """Take a group out of `matches`. Only do this in a paused() block, which renumbers the groups after it"""
        match_i = group.match_i
        logger.debug('Unmatching match group %s', match_i)
        group.match_i = None

        # Groups aren't renumbered until the paused() block ends, so this group's match_i is ahead of its place
        #   in `matches` by how many groups before it were already taken out in this block
        del self.matches[match_i - bisect_left(self._unmatched, match_i)]
        insort(self._unmatched, match_i)
        self.duplicate_images -= len(group.matches)

    def _update_match(self, group: ImageMatch):
        """Make a group a match or stop it being one, as _counts_as_match decides, after its images changed"""
        if self._counts_as_match(group):
            if group.match_i is None:
                self._match(group)
        elif group.match_i is not None:
            self._unmatch(group)

    def _find_group(self, hashes: set) -> int | str | None:
        """
        Find the group an image belongs to.

        Args:
            hashes (set): All of the image's hashes (every rotation and mirror of it).

        Returns:
            int | str | None: The key in _hashes of the closest group, or None if no group is close enough.
        """
        if self._index is None:
            # From testing at ~1.5m loaded images: it is ~10% faster to return a set and do this than it is to
            #   iterate over a list and do an `is in` check for each hash
            found_hashes = self._hashes.keys() & hashes
            return min(found_hashes) if found_hashes else None

        closest = None
        for hash_ in hashes:
            for found_hash, distance in self._index.find(hash_).items():
                if closest is None or (distance, found_hash) < closest:
                    closest = (distance, found_hash)

        return None if closest is None else closest[1]

    def _process_image_callback(self, result):
        """
        Handle the result of hashing an image, and update the found and processed image counts.

        This runs on the pool's thread that hands back results. If it raised, that thread would stop, so no more results
            would be handled and the scan would never finish. So an image that can't be added is logged,
            and counted like an image that couldn't be hashed.

        Args:
            result: A tuple consisting of the path to the file, and the resultant hashes.
                If the hashes are a dict, then it is assumed that the path is for a zip. In that case,
                the individual zip files will sanitized and re-ran through this callback.
        """
        # TODO: This callback must return IMMEDIATELY and is currently too slow for large amounts of zips.
        #   Perhaps create a new queue/thread and queue up processing for zip results?
        #   I think the major slow point is adding to the data structures and I'm not sure if more threads will help
        if isinstance(result[1], dict):
            try:
                self._add_zip_result(result)
            except Exception:
                logger.exception("Could not add the images in %s", result[0])
                with self._lock:
                    self.processed_images += 1  # Counted like a zip that couldn't be read
            return

        # Check for paused or finished signals
        self._not_paused.wait()
        with self._lock:  # Whatever paused to change or read the data (see paused) holds this until it's done
            if self.is_finished():
                return

            try:
                processed = self._add_result(result)
            except Exception:
                logger.exception("Could not add %s", result[0])
                processed = True

            if processed:
                self.processed_images += 1
            else:
                self.found_images -= 1

    def _add_zip_result(self, result):
        """
        Add the images that were hashed in a zip, by passing each one to _process_image_callback on its own.

        That waits for any pause to end before each image, so the lock is only held for one image at a time.
            Holding it for the whole zip would leave whatever paused waiting for the lock, while this waits for the
            pause to end.
        The zip stays counted as a file left to process until all of its images are added and counted,
            so the scan can't finish partway through it.
        """
        path, hashes = result
        with self._lock:
            if self.is_finished():
                return

            if self._is_removed(path):
                # This zip was removed AFTER it was queue'd! So just leave...
                self.found_images -= 1
                return

        subpaths = []
        for sub_path, sub_hashes in hashes.items():
            with self._lock:
                self.found_images += 1
            subpaths.append(ZipPath(str(path), sub_path))
            self._process_image_callback((subpaths[-1], sub_hashes))

        with self._lock:
            self._processed_zips[str(path)] = subpaths
            self.found_images -= 1

    def _add_result(self, result) -> bool:
        """
        Add the result of hashing an image (not a zip) to the matching. The lock must be held.

        This needs to do quite a few things including sanitizing the results,
            actually checking if the hash matches an existing image,
            adding the image and any matches to the backend data structures, notify any listeners,
            and verify that this result wasn't added as a removed path since it was queued.

        Returns:
            bool: Whether the image was processed. False if it was skipped, so it isn't part of the scan after all.
        """
        # region Sanitize results
        path: Path | str | ZipPath
        path, hashes = result

        if self._is_removed(path.path if isinstance(path, ZipPath) else path):
            # This image was removed AFTER it was queue'd! So just leave...
            return False

        initial_hash, extra_hashes = hashes
        extra_hashes.add(initial_hash)
        if not isinstance(path, ZipPath):
            # From this point on, EVERYTHING should be a ZipPath
            path = ZipPath(str(path), "")
        # endregion

        if path in self._reverse_hashes:
            return False

        hash_ = self._find_group(extra_hashes)
        if hash_ is None:
            # This is a new image not matching any previous, so just add it to the hashmap and move on...
            #   Just use the initial orientation
            hash_ = initial_hash
            self._reverse_hashes[path] = hash_
            self._hashes[hash_].matches.append(path)
            if self._index is not None:
                self._index.add(hash_)
            return True

        # We have found a match!
        group = self._hashes[hash_]
        self._reverse_hashes[path] = hash_
        group.matches.append(path)

        if group.match_i is not None:
            # Just another match for an existing group...
            self.duplicate_images += 1
            self.events_sent += 1
            self.events.put(NewMatch(group.snapshot(), path, self.events_sent))
            logger.debug('New match found for group #%s: %s', group.match_i, group.matches)
        elif self._counts_as_match(group):
            # This is a brand new match group! (Or one that the group filters dropped until this image was added)
            self._match(group)
            self.events_sent += 1
            self.events.put(NewGroup(group.snapshot(), self.events_sent))
            logger.debug('New match group found: %s', group.matches)

        return True

    def _process_image_error_callback(self, e):
        """Log an image that couldn't be hashed, and count it as processed"""
        with self._lock:
            self.processed_images += 1
        # Errors from _process_image say which file they were for, but errors from the pool itself (like a result that
        #   couldn't be sent back) don't. And like _process_image_callback, this must not raise
        logger.error("%s: %s (input path %s)", type(e).__name__, e, getattr(e, "input_path", "unknown"))

    def _on_task_done(self, result):
        """The pool's callback for a file it hashed: add the result, then make room for another file"""
        try:
            self._process_image_callback(result)
        finally:
            self._free_slots.release()

    def _on_task_error(self, e):
        """The pool's callback for a file it couldn't hash: log it, then make room for another file"""
        try:
            self._process_image_error_callback(e)
        finally:
            self._free_slots.release()

    def _root_stream(self, wait):
        """
        This is to yield any paths for processing, then wait until processing is finished for any new paths.

        Args:
            wait (Callable[[], None]): Waits a little (~50ms), for when there are no new paths.

        Yields:
            str: The paths to process.
        """
        # Once finished, images that are still being hashed are dropped rather than counted, so don't wait for them
        while not self.is_finished() and (not self._new_paths.empty() or self.left_to_process):
            if self._new_paths.empty():
                wait()
                continue

            yield self._new_paths.get_nowait()

    def _send_files(self, tp, unsent: deque, *, wait: float = 0):
        """
        Hand files the walk found to the pool to hash, while it has room for them (see TASKS_PER_PROCESS).

        Files in a folder that was removed from processing since they were found are skipped (see remove_path).

        Args:
            tp (multiprocessing.pool.Pool): The pool.
            unsent (deque[str]): The paths of the files waiting to be handed over, oldest first.
            wait (float): How long to wait for room, in seconds, if there isn't any. With no files waiting,
                this just waits that long.
        """
        if not unsent:
            time.sleep(wait)
            return

        while unsent and not self.is_finished():
            # Only wait for room for the first file
            if not (self._free_slots.acquire(timeout=wait) if wait else self._free_slots.acquire(blocking=False)):
                return
            wait = 0

            path = unsent.popleft()
            if self._is_removed(path):
                self._free_slots.release()
                with self._lock:  # The result thread changes this count too
                    self.found_images -= 1
                continue

            tp.apply_async(
                _process_image,
                args=(path, ),
                kwds={
                    'supported_extensions': self.extensions,
                    'exact_match': self.exact_match,
                    'draft': self.radius >= DRAFT_RADIUS,
                },
                callback=self._on_task_done,
                error_callback=self._on_task_error,
            )

    def run(self, paths: list[str | Path]):
        """Do the work of matching!"""
        self._not_paused.set()
        self._finished.clear()

        for path in paths:
            self.add_path(path)

        processes = self.processes or os.cpu_count() or 1
        self._free_slots = Semaphore(processes * TASKS_PER_PROCESS)
        unsent = deque()  # The files found that are waiting to be handed to the pool, see _send_files
        with Pool(processes,
                  initializer=_init_worker, initargs=(ImageFile.LOAD_TRUNCATED_IMAGES, )) as tp:
            # Waiting for results is when the files that were waiting for room in the pool are handed over
            for path in self._root_stream(partial(self._send_files, tp, unsent, wait=0.05)):
                path = Path(path)
                if not path.is_dir():
                    logger.warning('A path was entered that was not a directory : %s', path)
                    continue

                path = str(path.expanduser().resolve())
                if path in self._removed_paths:
                    continue

                for root, dirs, files in os.walk(path):
                    if self.is_finished():
                        break

                    dirs.sort()  # This actually works to ensure that os.walk goes in alphabetical order!
                    root = Path(root)

                    if self._is_removed(root):
                        continue

                    for f in files:
                        self._not_paused.wait()
                        if self.is_finished():
                            break

                        f = root / f

                        if f.suffix.lower() not in self.extensions:
                            continue

                        if str(f) in self._ignored_files:
                            continue

                        if f.suffix.lower() == '.zip':
                            if str(f.expanduser().resolve()) in self._processed_zips:
                                continue
                        elif ZipPath(str(f), "") in self._reverse_hashes:
                            continue

                        with self._lock:  # The result thread changes this count too
                            self.found_images += 1
                        # If its folder is removed from processing, it's skipped when it's handed over
                        unsent.append(str(f))
                        self._send_files(tp, unsent)

            tp.close()

            if not self.is_finished():
                tp.join()

        if not self.is_finished():
            self._finished.set()
            self.events.put(Finished())
