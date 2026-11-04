"""Read text and QR codes from photos of paperwork.

OCR uses Tesseract through pytesseract. QR decoding uses zxing-cpp. Both are
optional: if either is missing the app still runs and simply asks the person
to type the number instead.
"""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse absurdly large images
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


class BadImage(ValueError):
    pass


@lru_cache(maxsize=1)
def tesseract_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def qr_available() -> bool:
    try:
        import zxingcpp  # noqa: F401

        return True
    except Exception:
        return False


def load_image(data: bytes) -> Image.Image:
    if not data:
        raise BadImage("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BadImage("image is larger than 8 MB")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise BadImage(f"not a readable image ({e.__class__.__name__})") from e
    img = ImageOps.exif_transpose(img)  # phone photos carry their rotation in EXIF
    return img.convert("RGB")


def read_qr(img: Image.Image) -> list[str]:
    if not qr_available():
        return []
    import zxingcpp

    try:
        return [r.text for r in zxingcpp.read_barcodes(img) if r.text]
    except Exception:
        return []


def _prep(img: Image.Image) -> Image.Image:
    g = img.convert("L")
    if g.width < 1400:
        scale = 1400 / g.width
        g = g.resize((int(g.width * scale), int(g.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(g)


def read_text(img: Image.Image) -> str | None:
    """OCR the image. Returns None if OCR isn't available."""
    if not tesseract_available():
        return None
    import pytesseract

    g = _prep(img)
    text = pytesseract.image_to_string(g)
    # A second pass treating the page as one block helps with tilted photos.
    if sum(ch.isdigit() for ch in text) < 11:
        text += "\n" + pytesseract.image_to_string(g, config="--psm 6")
    return text
