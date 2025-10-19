# TODO: Add rotations!

import hashlib
import logging
import os
import time

from collections import defaultdict
from dataclasses import dataclass, field
from multiprocessing import Pool, Manager
from pathlib import Path
from threading import Event
from typing import Union

import imagehash

from PIL import Image

logger = logging.getLogger(__name__)

def phash_params_for_strength(strength: int) -> tuple[int, int]:
    """
    TODO: This sucks.

    Map 0..10 to (hash_size, highfreq_factor).
    - 10..9: very strict, use a bigger hash with default oversampling
    - 8..4: default (good balance)
    - 3..0: same bits but slightly lower oversampling (a touch looser)
    """
    strength = max(0, min(10, strength))
    if strength >= 9:
        return 16, 4    # 256-bit hash, strict
    elif strength >= 4:
        return 8, 4     # 64-bit hash, balanced
    else:
        return 8, 3     # same bits, slightly blurrier pre-DCT


def calculate_hash(file_path, strength=5, exact_match=False):
    if exact_match:
        hasher = hashlib.sha256()
        block_size = 65536
        with open(file_path, "rb") as file:
            for block in iter(lambda: file.read(block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()

    hash_size, highfreq_factor = phash_params_for_strength(strength)
    with Image.open(file_path) as im:
        return imagehash.phash(im, hash_size=hash_size, highfreq_factor=highfreq_factor)


def _process_image(path: str | Path):
    path = Path(path)
    return path, calculate_hash(path)


@dataclass
class ImageMatch:
    match_i: int | None = field(default=None)
    matches: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class NewGroup:
    group: "ImageMatch"  # forward-ref to your class


@dataclass(frozen=True)
class NewMatch:
    group: "ImageMatch"
    path: Path


@dataclass(frozen=True)
class Finished:
    pass


MatcherEvent = Union[NewGroup, NewMatch, Finished]


# TODO: FINISHED signal?
class ImageMatcher:
    SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif"}

    def __init__(self, strength: int = 5, exact_match: bool = False, processes: int | None = None,
                 extensions: set | None = None):
        if not (0 <= strength <= 10):
            raise ValueError("Strength must be between 0 and 10!")

        self.extensions = extensions or self.SUPPORTED_EXTS

        self.strength = strength
        self.exact_match = exact_match
        self.processes = processes
        self.found_images = 0
        self.processed_images = 0
        self.duplicate_images = 0

        m = Manager()
        self.events = m.Queue()
        self._hashes = defaultdict(ImageMatch)
        self._reverse_hashes = dict()

        self._conditional_pause = None
        self._not_paused = Event()
        self._not_paused.set()
        self._finished = Event()
        self._finished.set()

        self.matches = []

    @property
    def left_to_process(self):
        return self.found_images - self.processed_images

    def pause(self):
        logger.debug('Performing pause')
        self._not_paused.clear()

    def conditional_pause(self):
        self._conditional_pause = self.is_paused()
        if not self._conditional_pause:
            logger.debug('Performing conditional pause')
            self.pause()

    def conditional_resume(self):
        if not self._conditional_pause:
            logger.debug('Performing conditional resume')
            self.resume()

    def is_paused(self):
        return not self._not_paused.is_set()

    def finish(self):
        logger.debug('Performing finished')
        self._finished.set()

    def is_finished(self):
        return self._finished.is_set()

    def resume(self):
        logger.debug('Performing resume')
        self._not_paused.set()

    def remove(self, path):
        # Pause things while we remove things...
        logger.info('Removing %s from %s', path, self.__class__.__name__)
        self.conditional_pause()

        path = Path(path)
        hash = self._reverse_hashes.pop(path)
        self._hashes[hash].matches.remove(path)
        if len(self._hashes[hash].matches) == 1:
            match_i = self._hashes[hash].match_i
            logger.debug('Unmatching match group %s', match_i)
            self._hashes[hash].match_i = None

            del self.matches[match_i]
            self.refresh_match_indexes(match_i)
            self.duplicate_images -= 2

        elif not self._hashes[hash].matches:
            logger.debug('Removing empty match group')
            del self._hashes[hash]

        else:
            logger.debug('Simple removal performed')
            self.duplicate_images -= 1

        self.conditional_resume()

    def refresh_match_indexes(self, start=0):
        for match_i, match in enumerate(self.matches[start:], start=start):
            match.match_i = match_i

    def _process_image_callback(self, result):
        self._not_paused.wait()
        if self.is_finished():
            return

        path: Path
        path, hash = result

        self.processed_images += 1
        self._reverse_hashes[path] = hash
        if hash not in self._hashes:
            # This is a new hash, so just add it to the hashmap and move on...
            self._hashes[hash].matches.append(path)
            return

        # This appears to be a new match!
        for match in self._hashes[hash].matches:
            if path.absolute() == match.absolute():
                # This appears to be a duplicate PATH...
                logger.warning('Duplicate files entered! %s, %s', path, match)
                return

        self._hashes[hash].matches.append(path)
        if not self._hashes[hash].match_i and len(self._hashes[hash].matches) >= 2:
            # This is a brand new match group!
            self._hashes[hash].match_i = len(self.matches)
            self.matches.append(self._hashes[hash])
            self.duplicate_images += 2
            self.events.put(NewGroup(self._hashes[hash]))
            logger.debug('New match group found: %s', self._hashes[hash].matches)
        else:
            # Just another match for an existing group...
            self.duplicate_images += 1
            self.events.put(NewMatch(self._hashes[hash], path))
            logger.debug('New match found for group #%s: %s',
                         self._hashes[hash].match_i,
                         self._hashes[hash].matches)

    def _process_image_error_callback(self, e):
        self.processed_images += 1
        print(str(e))

    def run(self, paths: list[str | Path]):
        # TODO: Verify none of the paths overlap

        self._not_paused.set()
        self._finished.clear()

        with Pool(self.processes) as tp:
            for path in paths:
                if self.is_finished():
                    break

                for root, dirs, files in os.walk(path):
                    if self.is_finished():
                        break

                    for f in files:
                        self._not_paused.wait()
                        if self.is_finished():
                            break

                        f = Path(os.path.join(root, f))

                        if f.suffix.lower() not in self.extensions:
                            continue

                        self.found_images += 1
                        tp.apply_async(
                            _process_image,
                            args=(f, ),
                            callback=self._process_image_callback,
                            error_callback=self._process_image_error_callback,
                        )

            tp.close()
            tp.join()

        self._finished.set()
        self.events.put(Finished())
