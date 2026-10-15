"""Server-side image handling for trip photos (Pillow): re-encode every upload to a clean JPEG and make thumbnails, plus the
small in-process byte LRU the media endpoint serves from. Contract: docs/GDRIVE_MEDIA.md.

Re-encode = honour the EXIF orientation (ImageOps.exif_transpose), then drop ALL metadata (EXIF incl. GPS, XMP, comments): the
saved JPEG carries pixels only. The capture time is kept in our own metadata (Drive appProperties / description), never in EXIF.
"""
from __future__ import annotations

import io
import threading
from collections import OrderedDict

from PIL import Image, ImageOps, UnidentifiedImageError

FULL_MAX_SIDE = 1600
THUMB_MAX_SIDE = 320
JPEG_QUALITY = 82
THUMB_QUALITY = 75
MAX_PIXELS = 50_000_000  # a 5 MiB upload that decodes to more than this is a decompression bomb, not a phone photo


class ImageError(Exception):
    """The bytes are not a decodable image (or are absurdly large once decoded)."""


def _open(data: bytes, draft_side: int | None = None) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise ImageError("image is too large")
        if draft_side and image.format == "JPEG":
            image.draft("RGB", (draft_side, draft_side))  # decode a phone JPEG at reduced scale: much less memory / CPU
        image.load()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError(f"not a decodable image: {type(exc).__name__}") from exc
    return image


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))  # transparent PNG / WebP areas become white, not black
        flat.paste(rgba, mask=rgba.split()[-1])
        return flat
    return image.convert("RGB") if image.mode != "RGB" else image


def _encode(image: Image.Image, max_side: int, quality: int) -> tuple[bytes, int, int]:
    image = _to_rgb(image)
    if max(image.size) > max_side:
        image = image.copy()
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    clean = Image.new("RGB", image.size)  # a fresh image: no info dict, so no EXIF / XMP / ICC / comment can leak into the output
    clean.paste(image)
    out = io.BytesIO()
    clean.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue(), clean.width, clean.height


def reencode_jpeg(data: bytes, max_side: int = FULL_MAX_SIDE, quality: int = JPEG_QUALITY) -> tuple[bytes, int, int]:
    """(jpeg bytes, width, height): upright (EXIF orientation applied), longest side <= max_side, no metadata at all."""
    image = _open(data, draft_side=max_side)
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:  # a broken EXIF block must not lose the photo: keep it as stored
        pass
    return _encode(image, max_side, quality)


def thumbnail_jpeg(data: bytes, max_side: int = THUMB_MAX_SIDE) -> bytes:
    """A small JPEG of an already re-encoded image (longest side <= max_side)."""
    return _encode(_open(data), max_side, THUMB_QUALITY)[0]


class ByteLRU:
    """Thread-safe LRU of bytes values capped by total size (not count). Instance-local: each worker process has its own."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        if len(value) > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._items[key] = value
            self._size += len(value)
            while self._size > self.max_bytes and self._items:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0

    @property
    def size(self) -> int:
        return self._size
