"""Draw air waybill and insurance certificate images with Pillow.

Used for the fake documents in data/fakes/, genuine sample AWBs, and the
certificate a customer receives when a policy is issued. Layouts are generic
and invented; they do not copy any real airline's or insurer's documents.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Iterable

import qrcode
from PIL import Image, ImageDraw, ImageFont

INK = (28, 33, 40)
MUTED = (96, 104, 115)
RULE = (150, 158, 168)
RED = (176, 32, 38)
PAPER = (255, 255, 255)
TINT = (243, 246, 249)

SYNTHETIC_NOTE = "Synthetic test document generated for the CargoShield demo. Not a real air waybill."


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _box(d: ImageDraw.ImageDraw, xy, title: str, lines: Iterable[str], size: int = 26) -> None:
    x0, y0, x1, y1 = xy
    d.rectangle(xy, outline=RULE, width=2)
    d.text((x0 + 14, y0 + 10), title, font=font(18), fill=MUTED)
    y = y0 + 40
    for line in lines:
        d.text((x0 + 14, y), line, font=font(size), fill=INK)
        y += size + 10


def qr_image(data: str, box_size: int = 8) -> Image.Image:
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=2)
    q.add_data(data)
    q.make(fit=True)
    return q.make_image(fill_color="black", back_color="white").convert("RGB")


def draw_awb(
    awb_display: str,
    shipper: str,
    shipper_city: str,
    consignee: str,
    consignee_city: str,
    origin: str,
    destination: str,
    flight_date: date | str,
    pieces: int,
    weight_kg: float,
    goods: str,
    customs_value_usd: float,
    carrier: str = "Demo Airline",
    stamp: str | None = None,
    scam_note: str | None = None,
    note: str = SYNTHETIC_NOTE,
) -> Image.Image:
    w, h = 1600, 1040
    img = Image.new("RGB", (w, h), PAPER)
    d = ImageDraw.Draw(img)

    # Header: number top-left, title top-right (the usual AWB arrangement)
    d.text((40, 30), awb_display, font=font(54), fill=INK, stroke_width=1, stroke_fill=INK)
    d.text((w - 520, 30), "AIR WAYBILL", font=font(46), fill=INK, stroke_width=1, stroke_fill=INK)
    d.text((w - 520, 88), "Not negotiable", font=font(22), fill=MUTED)
    d.text((40, 100), f"Issued by {carrier}", font=font(24), fill=MUTED)
    d.line((40, 140, w - 40, 140), fill=INK, width=3)

    _box(d, (40, 160, 790, 330), "Shipper's name and address", [shipper, shipper_city])
    _box(d, (810, 160, w - 40, 330), "Consignee's name and address", [consignee, consignee_city])
    _box(d, (40, 350, 520, 470), "Airport of departure", [origin])
    _box(d, (540, 350, 1020, 470), "Airport of destination", [destination])
    fd = flight_date if isinstance(flight_date, str) else flight_date.strftime("%d %b %Y").upper()
    _box(d, (1040, 350, w - 40, 470), "Requested flight date", [fd])

    # Goods table
    top = 500
    d.rectangle((40, top, w - 40, top + 220), outline=RULE, width=2)
    d.rectangle((40, top, w - 40, top + 44), fill=TINT, outline=RULE, width=2)
    cols = [(40, "No. of pieces"), (300, "Gross weight (kg)"), (620, "Nature and quantity of goods")]
    for x, title in cols:
        d.text((x + 14, top + 10), title, font=font(20), fill=MUTED)
    for x in (300, 620):
        d.line((x, top, x, top + 220), fill=RULE, width=2)
    d.text((54, top + 80), str(pieces), font=font(34), fill=INK)
    d.text((314, top + 80), f"{weight_kg:,.1f}", font=font(34), fill=INK)
    d.text((634, top + 80), goods, font=font(34), fill=INK)

    _box(d, (40, 740, 790, 850), "Declared value for carriage", ["NVD"])
    _box(d, (810, 740, w - 40, 850), "Declared value for customs", [f"USD {customs_value_usd:,.0f}"])

    if scam_note:
        d.rectangle((40, 870, 1100, 950), outline=RED, width=3)
        d.text((56, 892), scam_note, font=font(24), fill=RED)

    if stamp:
        f = font(34)
        tw = int(d.textlength(stamp, font=f))
        sw, sh = tw + 60, 96
        st = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        sd = ImageDraw.Draw(st)
        sd.rectangle((4, 4, sw - 4, sh - 4), outline=RED + (210,), width=6)
        sd.text((30, 28), stamp, font=f, fill=RED + (210,))
        st = st.rotate(6, expand=True)
        img.paste(st, (w - st.width - 50, 862), st)

    d.text((40, h - 52), awb_display, font=font(26), fill=INK)
    d.text((360, h - 46), note, font=font(18), fill=MUTED)
    return img


def draw_certificate(
    title: str,
    policy_no: str,
    awb_display: str,
    insured: str,
    route: str,
    flight_date: str,
    goods: str,
    sum_insured_usd: float,
    premium_usd: float | None,
    issued_on: str,
    qr_data: str | None = None,
    verify_hint: str | None = None,
    issuer: str = "CargoShield cover",
    note: str = "Synthetic document for the CargoShield demo. Illustrative cover, not a real policy.",
) -> Image.Image:
    w, h = 1240, 1600
    img = Image.new("RGB", (w, h), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle((24, 24, w - 24, h - 24), outline=INK, width=4)
    d.text((70, 70), issuer.upper(), font=font(26), fill=MUTED)
    d.text((70, 120), title, font=font(50), fill=INK, stroke_width=1, stroke_fill=INK)
    d.line((70, 200, w - 70, 200), fill=INK, width=3)

    rows = [
        ("Policy number", policy_no),
        ("Air waybill", awb_display),
        ("Insured", insured),
        ("Route", route),
        ("Flight date", flight_date),
        ("Goods", goods),
        ("Sum insured", f"USD {sum_insured_usd:,.2f}"),
    ]
    if premium_usd is not None:
        rows.append(("Premium", f"USD {premium_usd:,.2f}"))
    rows.append(("Issued on", issued_on))
    y = 240
    for k, v in rows:
        d.text((70, y), k, font=font(26), fill=MUTED)
        d.text((420, y), v, font=font(34), fill=INK)
        y += 74

    if qr_data:
        qr = qr_image(qr_data, box_size=9).resize((420, 420), Image.NEAREST)
        img.paste(qr, (70, y + 30))
        d.text((520, y + 60), "Scan to verify this cover", font=font(30), fill=INK)
        if verify_hint:
            for i, line in enumerate(verify_hint.split("\n")):
                d.text((520, y + 110 + i * 40), line, font=font(24), fill=MUTED)

    d.text((70, h - 90), note, font=font(20), fill=MUTED)
    return img


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
