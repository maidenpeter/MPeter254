"""Air waybill numbers.

Format: 3-digit airline prefix + 8-digit serial. The 8th digit of the serial
is a check digit: the first 7 digits of the serial, mod 7.

    999-1234567 5   ->  1234567 mod 7 = 5, so 999-12345675 is well formed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 3 digits, optional separator, 8 digits. The 8 are usually printed 4 + 4, but
# people and OCR split them anywhere ("2429 6742", "24296 742"), so allow up to
# two single spaces or dots inside the serial.
_AWB_RE = re.compile(r"(?<![0-9])([0-9]{3})[ \t]*[-–.]?[ \t]*((?:[0-9][ .]?){7}[0-9])(?![0-9])")

# Characters OCR commonly confuses with digits inside a number.
_OCR_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1",
                          "i": "1", "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6"})
_DIGITISH = re.compile(r"[0-9OoDQIl|iSsBZzG][0-9OoDQIl|iSsBZzG\-–.\s]{9,18}[0-9OoDQIl|iSsBZzG]")


def check_digit(serial7: str) -> int:
    if len(serial7) != 7 or not serial7.isdigit():
        raise ValueError("serial7 must be 7 digits")
    return int(serial7) % 7


def make(prefix: str, serial7: str) -> str:
    """Build a valid 11-digit AWB from a prefix and a 7-digit serial."""
    return f"{prefix}{serial7}{check_digit(serial7)}"


def make_invalid(prefix: str, serial7: str) -> str:
    """Build an AWB whose check digit is deliberately wrong (for fake documents)."""
    bad = (check_digit(serial7) + 3) % 10  # always differs from the real check digit (0-6)
    return f"{prefix}{serial7}{bad}"


def example(prefix: str) -> str:
    """A well-formed sample number for help text, e.g. 999-12345675."""
    return fmt(make(prefix, "1234567"))


def fmt(awb11: str) -> str:
    """999-12345675 is the usual printed form."""
    return f"{awb11[:3]}-{awb11[3:]}" if len(awb11) == 11 else awb11


@dataclass(frozen=True)
class AwbCheck:
    status: str  # "ok" | "bad_format" | "bad_check_digit"
    awb: str | None  # 11 digits when the input could be parsed
    raw: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def prefix(self) -> str | None:
        return self.awb[:3] if self.awb else None


def normalise(text: str) -> str | None:
    """Return 11 digits if the text is exactly one AWB-shaped number, else None."""
    digits = re.sub(r"[\s\-–.]", "", text or "")
    # ASCII only: Python's isdigit() also accepts superscripts and other scripts' digits.
    return digits if len(digits) == 11 and digits.isascii() and digits.isdigit() else None


def validate(text: str) -> AwbCheck:
    awb = normalise(text)
    if awb is None:
        return AwbCheck("bad_format", None, text)
    serial = awb[3:]
    if int(serial[7]) != check_digit(serial[:7]):
        return AwbCheck("bad_check_digit", awb, text)
    return AwbCheck("ok", awb, text)


def find_in_text(text: str, fix_ocr: bool = False) -> list[str]:
    """Find AWB-shaped numbers in free text (chat messages, OCR output).

    Returns 11-digit strings in the order found, without duplicates. With
    fix_ocr=True it also repairs common OCR slips (O->0, l->1, S->5...) inside
    number-like runs before matching.
    """
    text = text or ""
    candidates: list[str] = []
    sources = [text]
    if fix_ocr:
        fixed = _DIGITISH.sub(lambda m: m.group(0).translate(_OCR_FIX), text)
        sources.append(fixed)
    for src in sources:
        for m in _AWB_RE.finditer(src):
            serial = m.group(2)
            if sum(not ch.isdigit() for ch in serial) > 2:
                continue  # "1 2 3 4 5 6 7 8" is not how anyone writes an AWB
            awb = m.group(1) + re.sub(r"[ .]", "", serial)
            if awb not in candidates:
                candidates.append(awb)
    return candidates
