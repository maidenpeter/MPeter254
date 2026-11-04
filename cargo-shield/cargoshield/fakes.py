"""Fake documents for the demo (data/fakes/).

Each fake uses an AWB number that either fails the check digit or does not
exist in the bookings table. The seed never issues these serials, so they can
never collide with a real (synthetic) booking. The layouts are invented here;
nothing is copied from real forged documents. Only the scam *patterns* are
borrowed: a "clearing fee" note, a release stamp, a QR code that points to a
look-alike site.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter

from . import awb as awbmod
from . import config, documents


@dataclass(frozen=True)
class FakeSpec:
    filename: str
    kind: str  # "awb" | "certificate"
    serial7: str
    valid_check_digit: bool
    expected: str  # what CargoShield should answer
    story: str


FAKES: list[FakeSpec] = [
    FakeSpec("fake_awb_clearance_fee.png", "awb", "4718203", True, "NOT FOUND",
             "Buyer is told the phones have landed and must pay a clearing fee to an agent."),
    FakeSpec("fake_awb_gold_dore.png", "awb", "5093371", True, "NOT FOUND",
             "Gold shipment 'proof' used to pull an advance payment from an investor."),
    FakeSpec("fake_awb_bad_check_phones.png", "awb", "3367024", False, "SUSPICIOUS",
             "Number fails the check digit, so the airline could never have issued it."),
    FakeSpec("fake_awb_bad_check_generators.png", "awb", "6620518", False, "SUSPICIOUS",
             "Machinery shipment with an impossible AWB number."),
    FakeSpec("fake_awb_phone_photo.jpg", "awb", "2284650", True, "NOT FOUND",
             "Same scam, sent as a tilted phone photo to show OCR copes with real pictures."),
    FakeSpec("fake_cert_no_booking.png", "certificate", "7731294", True, "NOT FOUND",
             "Insurance certificate for a booking that does not exist."),
    FakeSpec("fake_cert_bad_check.png", "certificate", "1845562", False, "SUSPICIOUS",
             "Insurance certificate quoting an impossible AWB number."),
    FakeSpec("fake_cert_lookalike_qr.png", "certificate", "8350716", True, "SUSPICIOUS",
             "Certificate whose QR code points to a look-alike verification site."),
]

# The seed must never generate these serials.
RESERVED_SERIALS = {f.serial7 for f in FAKES}

DEMO_FAKE = FAKES[0].filename  # used in step 1 of DEMO.md


def fake_awb(spec: FakeSpec, prefix: str | None = None) -> str:
    prefix = prefix or config.awb_prefix()
    return awbmod.make(prefix, spec.serial7) if spec.valid_check_digit else awbmod.make_invalid(prefix, spec.serial7)


def _photo_look(img: Image.Image) -> Image.Image:
    """Make a clean render look like a quick phone photo: tilt, blur, grain."""
    img = img.convert("L").rotate(1.4, expand=True, fillcolor=228, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    noise = Image.effect_noise(img.size, 18).convert("L")
    img = Image.blend(img, noise, 0.08)
    return img.convert("RGB")


def _render(spec: FakeSpec, prefix: str) -> Image.Image:
    number = awbmod.fmt(fake_awb(spec, prefix))
    if spec.filename == "fake_awb_clearance_fee.png":
        return documents.draw_awb(number, "Upendo General Merchants", "Nairobi, Kenya",
                                  "Kito Hardware Stores", "Lagos, Nigeria", "NBO NAIROBI", "LOS LAGOS",
                                  "14 NOV 2026", 24, 340.0, "Mobile phones", 186000,
                                  stamp="CLEARED FOR RELEASE",
                                  scam_note="Release on payment of USD 1,850 clearing fee to agent")
    if spec.filename == "fake_awb_gold_dore.png":
        return documents.draw_awb(number, "Neema Minerals Ltd", "Kampala, Uganda",
                                  "Bahari Bullion Trading", "Hong Kong", "NBO NAIROBI", "HKG HONG KONG",
                                  "18 NOV 2026", 5, 25.0, "Gold dore", 1950000, stamp="PAID")
    if spec.filename == "fake_awb_bad_check_phones.png":
        return documents.draw_awb(number, "Imara Devices Co. Ltd", "Hong Kong",
                                  "Zuri Mobile Ltd", "Nairobi, Kenya", "HKG HONG KONG", "NBO NAIROBI",
                                  "11 NOV 2026", 40, 520.0, "Smartphones", 412000,
                                  scam_note="Balance of USD 9,500 due before goods are released")
    if spec.filename == "fake_awb_bad_check_generators.png":
        return documents.draw_awb(number, "Mlima Engineering Ltd", "Nairobi, Kenya",
                                  "Fahari Power SARL", "Kinshasa, DR Congo", "NBO NAIROBI", "FIH KINSHASA",
                                  "20 NOV 2026", 6, 1200.0, "Diesel generators", 64000, stamp="LOADED")
    if spec.filename == "fake_awb_phone_photo.jpg":
        return _photo_look(documents.draw_awb(
            number, "Tausi Trading Ltd", "Dar es Salaam, Tanzania", "Jua Electronics", "Nairobi, Kenya",
            "DAR DAR ES SALAAM", "NBO NAIROBI", "16 NOV 2026", 18, 260.0, "Laptop computers", 98000,
            stamp="CLEARED", scam_note="Pay storage charges to agent to avoid auction"))
    if spec.filename == "fake_cert_no_booking.png":
        return documents.draw_certificate(
            "Certificate of cargo insurance", "CSP-0904417", number, "Kito Hardware Stores",
            "NBO to LOS", "14 NOV 2026", "Mobile phones", 186000, 2790, "10 NOV 2026",
            issuer="Cargo insurance")
    if spec.filename == "fake_cert_bad_check.png":
        return documents.draw_certificate(
            "Certificate of cargo insurance", "CSP-0771025", number, "Zuri Mobile Ltd",
            "HKG to NBO", "11 NOV 2026", "Smartphones", 412000, 6180, "08 NOV 2026",
            issuer="Cargo insurance")
    if spec.filename == "fake_cert_lookalike_qr.png":
        return documents.draw_certificate(
            "Certificate of cargo insurance", "CSP-0662930", number, "Bahari Bullion Trading",
            "NBO to HKG", "18 NOV 2026", "Gold dore", 1950000, 29250, "15 NOV 2026",
            qr_data="https://cargoshield-verify.example/p/9KX2PQ",
            verify_hint="cargoshield-verify.example", issuer="CargoShield cover")
    raise ValueError(spec.filename)


def generate(out_dir: Path | None = None, prefix: str | None = None) -> list[Path]:
    out_dir = Path(out_dir or config.fakes_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = prefix or config.awb_prefix()
    paths = []
    for spec in FAKES:
        p = out_dir / spec.filename
        img = _render(spec, prefix)
        if p.suffix.lower() == ".jpg":
            img.save(p, format="JPEG", quality=82)  # phone photos arrive as JPEG
        else:
            img.save(p, format="PNG", optimize=True)
        paths.append(p)
    return paths


def by_filename(name: str) -> FakeSpec | None:
    return next((f for f in FAKES if f.filename == name), None)
