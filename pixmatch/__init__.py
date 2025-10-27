# TODO: If a folder is processed, then a new folder is created in that folder,
#  and that sub-folder is added for processing, it will never be ingested since its a subfolder of a processed folder.
#   I'm not sure of a good solution for this... Perhaps when re-running, we should rescan all ingestion folders for new files?
#   We'd actually need to keep track of ignored files though instead of just removing them from the system...

import hashlib
import logging
import os
import time

from collections import defaultdict
from dataclasses import dataclass, field
from multiprocessing import Manager, Pool
from pathlib import Path
from threading import Event
from typing import ClassVar, Union
from zipfile import ZipFile

import imagehash
import numpy as np

from PIL import Image

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
    subpath: str

    @property
    def path_obj(self) -> Path:
        """Get the path as as Path object"""
        return Path(self.path)

    @property
    def is_gif(self) -> bool:
        """Is this a path to an animated image?"""
        movie_extensions = {'.gif', '.webp'}
        return (not self.subpath and Path(self.path).suffix.lower() in movie_extensions) \
            or (self.subpath and self.subpath[-4:].lower() in movie_extensions)

    def absolute(self):
        """Get the absolute version of this ZipPath"""
        return ZipPath(str(self.path_obj.absolute()), self.subpath)


def _is_under(folder_abs: str, target: str | Path) -> bool:
    """Return True if the ZipPath's real file (zp.path) is inside folder_abs."""
    try:
        Path(target).absolute().relative_to(Path(folder_abs).absolute())
    except ValueError:
        return False

    return True


def phash_params_for_strength(strength: int) -> tuple[int, int]:
    """
    Convert a 0-10 strength to settings for imagehash

    Returns:
        tuple<int, int>: The hash size (in bytes) and the high frequency factor
    """
    # TODO: This sucks.
    strength = max(0, min(10, strength))
    if strength >= 10:
        return 16, 4
    if strength >= 8:
        return 15, 4
    if strength >= 7:
        return 13, 4
    if strength >= 6:
        return 11, 4
    if strength >= 5:
        return 9, 4
    if strength >= 4:
        return 8, 4
    if strength >= 3:
        return 8, 3
    if strength >= 2:
        return 7, 3
    return 6, 3


def calculate_hashes(f, strength=5, *, is_gif=False, exact_match=False) -> list:
    """
    Calculate hashes for a given file.

    Args:
        f (IO or str or Path): Either a file path to process, or a in-memory BytesIO object ready for reading.
        strength (int): A number between 0 and 10 on the strength of the matches.
        is_gif (bool): Is this gif data? Needed if passing an in-memory BytesIO object.
        exact_match (bool): Use exact SHA256 hahes?
            If true, strength must be 10.
            If false, perceptual hashes will be used, even with high strength.

    Returns:
        list: The found hashes.
    """
    if exact_match:
        hasher = hashlib.sha256()
        block_size = 65536
        with (open(f, "rb") if isinstance(f, (str, Path)) else f) as file:  # noqa: PTH123
            for block in iter(lambda: file.read(block_size), b""):
                hasher.update(block)
        return [hasher.hexdigest()]

    hash_size, highfreq_factor = phash_params_for_strength(strength)
    with Image.open(f) as im:
        if is_gif:
            initial_hash = imagehash.phash(im, hash_size=hash_size, highfreq_factor=highfreq_factor)
            # This is going to be a bit confusing but basically, imagehash produces weird hashes for some gifs
            #   because some gifs have bad first frames consisting of nothing or only a single color...
            # To deal with that I'm looking for these bad hashes here and if its one, we advance to the next frame
            #   and use THAT for imagehash instead.
            # The ones we need to be on the lookout for are:
            #   1. The hash is all 1111...
            #   2. The hash is all 0000...
            #   3. The hash is of the form 100000...
            # TODO: This is simply not good enough. I'm still getting bad matches for gifs, tho they are extremely rare
            val = initial_hash.hash[0][0]
            while all(all(x == val for x in r) for r in initial_hash.hash) \
                    or all(all(x == np.False_ or (x_i == 0 and r_i == 0) for x_i, x in enumerate(r))
                           for r_i, r in enumerate(initial_hash.hash)):
                try:
                    im.seek(im.tell() + 1)
                except EOFError:  # noqa: PERF203
                    break
                else:
                    initial_hash = imagehash.phash(im, hash_size=hash_size, highfreq_factor=highfreq_factor)
                    val = initial_hash.hash[0][0]

            # For GIFs we'll look for mirrored versions but thats it
            flipped_h_image = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            return [
                initial_hash,
                imagehash.phash(flipped_h_image, hash_size=hash_size, highfreq_factor=highfreq_factor),
            ]

        flipped_h_image = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        flipped_v_image = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        images = (im, im.rotate(90), im.rotate(180), im.rotate(270),
                  flipped_h_image, flipped_h_image.rotate(90), flipped_h_image.rotate(180), flipped_h_image.rotate(270),
                  flipped_v_image, flipped_v_image.rotate(90), flipped_v_image.rotate(180), flipped_v_image.rotate(270))
        return [imagehash.phash(image, hash_size=hash_size, highfreq_factor=highfreq_factor) for image in images]


def _process_image(
        path: str | Path,
        strength: int = 5,
        *,
        exact_match: bool = False,
) -> tuple[Path, list | dict[str, list]]:
    """Get the hashes for a given path. Is multiprocessing compatible"""
    path = Path(path)
    if path.suffix.lower() != '.zip':
        return path, calculate_hashes(path, is_gif=path.suffix.lower() in {".gif", ".webp"},
                                      strength=strength, exact_match=exact_match)

    results = {}
    with ZipFile(path) as zf:
        for f in zf.filelist:
            with zf.open(f) as zipped_file:
                results[f.filename] = calculate_hashes(zipped_file, is_gif=f.filename[-4:].lower() in {".gif", ".webp"},
                                                       strength=strength, exact_match=exact_match)

    return path, results


@dataclass
class ImageMatch:
    """A match data structure containing the matches and where this match lies in the match list"""
    match_i: int | None = field(default=None)
    matches: list[ZipPath] = field(default_factory=list)


# region Events
@dataclass(frozen=True)
class NewGroup:
    """A new group event"""
    group: "ImageMatch"


@dataclass(frozen=True)
class NewMatch:
    """A new match event"""
    group: "ImageMatch"
    path: ZipPath


@dataclass(frozen=True)
class Finished:
    """A finished event"""


MatcherEvent = Union[NewGroup, NewMatch, Finished]
# endregion


class ImageMatcher:
    """
    An image matching SDK

    Args:
        strength (int): The 0-10 strength to use for matching. Defaults to 5.
        exact_match (bool): Should use SHA-256 hashes? If False, the default, will use perceptual hashes.
            If True, strength must be 10.
        processes (int): The number of processes to use. Defaults to None.
        extensions (set): The extensions to process. Optional.
    """
    SUPPORTED_EXTS: ClassVar = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".zip"}

    def __init__(self, strength: int = 5, processes: int | None = None, extensions: set | None = None,
                 *, exact_match: bool = False):
        if not (0 <= strength <= 10):
            raise ValueError("Strength must be between 0 and 10!")

        self.extensions = extensions or self.SUPPORTED_EXTS

        self.strength = strength
        self.exact_match = exact_match
        self.processes = processes

        self.found_images = 0
        self.processed_images = 0
        self.duplicate_images = 0
        self.matches = []

        m = Manager()
        self.events = m.Queue()  # Events to go to higher level users
        self._new_paths = m.Queue()  # Inbound queue for new paths that are added while processing is running
        self._removed_paths = set()  # Paths that have been removed from processing after processing has been started
        self._processed_zips = set()  # Zips that have been successfully processed
        self._hashes = defaultdict(ImageMatch)  # Hash -> Paths
        self._reverse_hashes = {}  # Path -> Hash

        # Pausing and finished signaling...
        self._not_paused = Event()
        self._not_paused.set()
        self._finished = Event()
        self._finished.set()

    @property
    def left_to_process(self):
        """Files that are left to process"""
        return self.found_images - self.processed_images

    def add_path(self, path: str | Path):
        """Add a path for processing"""
        path = str(Path(path).absolute())
        self._removed_paths.discard(path)
        self._new_paths.put(path)

    def remove_path(self, folder: str | Path):
        """
        Mark a folder to be skipped going forward, and remove already-indexed files
        that live under it. Pauses briefly if not already paused to keep state sane.
        """
        # TODO: This works but the biggest problem with it is that it will not remove any images which are still
        #   queue'd up for processing in the ThreadPool... I'm not sure how to fix that yet.
        folder = str(Path(folder).absolute())
        paused = self.conditional_pause()
        self._removed_paths.add(folder)

        # Remove anything we've already seen under that folder
        # (iterate over a copy because remove() mutates structures)
        to_remove = [p for p in self._reverse_hashes if _is_under(folder, p.path)]
        for p in to_remove:
            self.remove(p)

        to_remove_zips = [p for p in self._processed_zips if _is_under(folder, p)]
        for p in to_remove_zips:
            self._processed_zips.remove(p)

        self.conditional_resume(paused)

    def conditional_pause(self) -> bool:
        """Pause if not paused and return if was paused"""
        _conditional_pause = self.is_paused()
        if not _conditional_pause:
            logger.debug('Performing conditional pause')
            self.pause()

        return _conditional_pause

    def conditional_resume(self, was_paused: bool):  # noqa: FBT001
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
        paused = self.conditional_pause()

        hash_ = self._reverse_hashes.pop(path)
        self._hashes[hash_].matches.remove(path)
        if len(self._hashes[hash_].matches) == 1:
            match_i = self._hashes[hash_].match_i
            logger.debug('Unmatching match group %s', match_i)
            self._hashes[hash_].match_i = None

            del self.matches[match_i]
            self.refresh_match_indexes(match_i)
            self.duplicate_images -= 2

        elif not self._hashes[hash_].matches:
            logger.debug('Removing empty match group')
            del self._hashes[hash_]

        else:
            logger.debug('Simple removal performed')
            self.duplicate_images -= 1

        self.processed_images -= 1
        self.found_images -= 1
        self.conditional_resume(paused)

    def refresh_match_indexes(self, start=0):
        """Update the match_i value for all the matches passed a certain point"""
        for match_i, match in enumerate(self.matches[start:], start=start):
            match.match_i = match_i

    def _process_image_callback(self, result):
        """
        Handle the result of hashing an image.

        This needs to do quite a few things including sanitizing the results,
            actually checking if the hash matches an existing image,
            adding the image and any matches to the backend data structures, notify any listeners,
            update the found and processed image counts,
            and verify that this result wasn't added as a removed path since it was queued.

        Args:
            result: A tuple consisting of the path to the file, and the resultant hashes.
                If the hashes are a dict, then it is assumed that the path is for a zip. In that case,
                the individual zip files will sanitized and re-ran through this callback.
        """
        # TODO: This callback must return IMMEDIATELY and is currently too slow for large amounts of zips.
        #   Perhaps create a new queue/thread and queue up processing for zip results?
        # Check for paused or finished signals
        self._not_paused.wait()
        if self.is_finished():
            return

        # region Sanitize results
        path: Path | str | ZipPath
        path, hashes = result

        if any(_is_under(d, path.path if isinstance(path, ZipPath) else path) for d in self._removed_paths):
            # This image was removed AFTER it was queue'd! So decrement the found images count and just leave...
            self.found_images -= 1
            return

        if isinstance(hashes, dict):
            self.found_images -= 1
            for sub_path, sub_hashes in hashes.items():
                self.found_images += 1
                self._process_image_callback((ZipPath(str(path), sub_path), sub_hashes))
            self._processed_zips.add(str(path))
            return

        if not isinstance(path, ZipPath):
            # From this point on, EVERYTHING should be a ZipPath
            path = ZipPath(str(path), "")
        # endregion

        if path in self._reverse_hashes:
            self.found_images -= 1
            return

        self.processed_images += 1
        for hash_ in hashes:
            # Of all the hashes returned, which may contain hashes for rotations/mirrors, look for matches...
            if hash_ not in self._hashes:
                continue

            # We have found a match!
            self._reverse_hashes[path] = hash_
            self._hashes[hash_].matches.append(path)

            if self._hashes[hash_].match_i is None and len(self._hashes[hash_].matches) >= 2:
                # This is a brand new match group!
                self._hashes[hash_].match_i = len(self.matches)
                self.matches.append(self._hashes[hash_])
                self.duplicate_images += 2
                self.events.put(NewGroup(self._hashes[hash_]))
                logger.debug('New match group found: %s', self._hashes[hash_].matches)
            else:
                # Just another match for an existing group...
                self.duplicate_images += 1
                self.events.put(NewMatch(self._hashes[hash_], path))
                logger.debug('New match found for group #%s: %s',
                             self._hashes[hash_].match_i,
                             self._hashes[hash_].matches)

            break
        else:
            # This is a new image not matching any previous, so just add it to the hashmap and move on...
            #   Just use the initial orientation
            hash_ = hashes[0]
            self._reverse_hashes[path] = hash_
            self._hashes[hash_].matches.append(path)
            return

    def _process_image_error_callback(self, e):
        """Temporary for testing"""
        self.processed_images += 1
        logger.error(str(e))

    def _root_stream(self):
        """This is to yield any paths for processing, then wait until processing is finished for any new paths"""
        while not self._new_paths.empty() or self.left_to_process:
            if self._new_paths.empty():
                time.sleep(0.05)
                continue

            yield self._new_paths.get_nowait()

    def run(self, paths: list[str | Path]):
        """Do the work of matching!"""
        self._not_paused.set()
        self._finished.clear()

        for path in paths:
            self.add_path(path)

        with Pool(self.processes) as tp:
            for path in self._root_stream():
                path = Path(path)
                if not path.is_dir():
                    logger.warning('A path was entered that was not a directory : %s', path)
                    continue

                path = str(path.absolute())
                if path in self._removed_paths:
                    continue

                for root, _, files in os.walk(path):
                    if self.is_finished():
                        break

                    root = Path(root)

                    if any(_is_under(d, root) for d in self._removed_paths):
                        continue

                    for f in files:
                        self._not_paused.wait()
                        if self.is_finished():
                            break

                        f = root / f

                        if f.suffix.lower() not in self.extensions:
                            continue

                        if any(_is_under(d, f) for d in self._removed_paths):
                            continue

                        if f.suffix.lower() == '.zip':
                            if str(f.absolute()) in self._processed_zips:
                                continue
                        elif ZipPath(str(f), "") in self._reverse_hashes:
                            continue

                        self.found_images += 1
                        tp.apply_async(
                            _process_image,
                            args=(f, ),
                            kwds={
                                'strength': self.strength,
                                'exact_match': self.exact_match,
                            },
                            callback=self._process_image_callback,
                            error_callback=self._process_image_error_callback,
                        )

            tp.close()

            if not self.is_finished():
                tp.join()

        if not self.is_finished():
            self._finished.set()
            self.events.put(Finished())
