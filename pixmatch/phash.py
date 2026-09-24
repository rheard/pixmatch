from functools import lru_cache

import numpy as np

from PIL import Image

HASH_SIZE = 8  # Hashes are HASH_SIZE x HASH_SIZE = 64 bits
HASH_IMG_SIZE = HASH_SIZE * 4  # Images are shrunk to this many pixels square to be hashed

# A frame whose pixels vary less than this (on a 0-255 scale, after resizing) is blank, like a GIF's empty first frame.
#   A blank frame has no detail for the hash to describe, so its hash bits would just be floating point noise.
FLAT_STD = 2.0

# Every rotation of a blank image is the same blank image, and its only meaningful bit is the first (DC) one
FLAT_HASH = 1 << (HASH_SIZE * HASH_SIZE - 1)

EXIF_ORIENTATION_TAG = 0x0112

# The numpy equivalent of the transpose that ImageOps.exif_transpose applies for each EXIF orientation
EXIF_ORIENTATIONS = {
    2: np.fliplr,  # FLIP_LEFT_RIGHT
    3: lambda px: np.rot90(px, 2),  # ROTATE_180
    4: np.flipud,  # FLIP_TOP_BOTTOM
    5: np.transpose,  # TRANSPOSE
    6: lambda px: np.rot90(px, 3),  # ROTATE_270
    7: lambda px: np.rot90(px, 2).T,  # TRANSVERSE
    8: lambda px: np.rot90(px, 1),  # ROTATE_90
}


@lru_cache
def _dct_basis(img_size: int, hash_size: int) -> np.ndarray:
    """The lowest frequency rows of an (unnormalized) DCT-II matrix, the same DCT imagehash's phash uses"""
    n = np.arange(img_size)
    k = np.arange(hash_size)[:, None]
    return 2 * np.cos(np.pi * (2 * n + 1) * k / (2 * img_size))


def grayscale_pixels(im: Image.Image, img_size: int) -> np.ndarray:
    """Convert an image (or the current frame of an animation) to an img_size x img_size grayscale array"""
    if im.mode in {"RGBA", "RGBa", "LA", "La", "PA"} or "transparency" in im.info:
        # Converting straight to grayscale throws away alpha and hashes whatever colors hide under transparent pixels.
        #   So put the image on a white background first, the same as a copy of it that was flattened would be
        im = im.convert("RGBA")
        im = Image.alpha_composite(Image.new("RGBA", im.size, "white"), im)
    elif im.mode.startswith("I;16"):
        # Pillow clips 16-bit values to 0-255 when converting to grayscale, which turns nearly everything white
        im = Image.fromarray((np.asarray(im) >> 8).astype(np.uint8))

    return np.asarray(im.convert("L").resize((img_size, img_size), Image.Resampling.LANCZOS), dtype=np.float64)


def is_flat(px: np.ndarray) -> bool:
    """Is this grayscale array blank (nothing, or only a single color)?"""
    return px.std() < FLAT_STD


def orientations(px: np.ndarray):
    """Yield the 8 rotations and mirrors of px, starting with px itself"""
    for k in range(4):
        rotated = np.rot90(px, k)
        yield rotated
        yield np.fliplr(rotated)


def phash(px: np.ndarray) -> int:
    """
    Perceptual hash of an already resized grayscale array.

    This is the same algorithm (and gives the same bits) as imagehash's phash.

    Returns:
        int: The 64 hash bits, in row-major order with the first bit being the most significant.
    """
    basis = _dct_basis(px.shape[0], HASH_SIZE)
    dct = basis @ px @ basis.T
    bits = (dct > np.median(dct)).ravel()
    return int.from_bytes(np.packbits(bits).tobytes(), "big")
