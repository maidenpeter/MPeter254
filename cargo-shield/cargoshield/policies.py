"""Issue cover and produce its QR code and certificate (Milestone 4)."""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import urlparse

from . import awb as awbmod
from . import db, documents, pricing, risk

_POLICY_NO_RE = re.compile(r"\bCSP[-\s]?(\d{5,8})\b", re.IGNORECASE)
_TOKEN_PATH_RE = re.compile(r"/verify/policy/([A-Za-z0-9_\-]{16,64})")


class CoverError(Exception):
    """The booking can't be covered (HIGH risk, under review, no top-up needed...)."""


def policy_no(pid: int) -> str:
    return f"CSP-{pid:07d}"


def parse_policy_no(text: str) -> int | None:
    m = _POLICY_NO_RE.search(text or "")
    return int(m.group(1)) if m else None


def verify_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/verify/policy/{token}"


def token_from_url(text: str) -> tuple[str | None, str | None]:
    """(token, host) if the text holds a CargoShield policy verification link."""
    m = _TOKEN_PATH_RE.search(text or "")
    if not m:
        return None, None
    host = urlparse(text.strip()).hostname if "://" in (text or "") else None
    return m.group(1), host


def to_dict(row: sqlite3.Row | dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
    d = {
        "id": row["id"],
        "policy_no": policy_no(row["id"]),
        "booking_id": row["booking_id"],
        "sum_insured_usd": row["sum_insured_usd"],
        "premium_usd": row["premium_usd"],
        "status": row["status"],
        "issued_at": row["issued_at"],
    }
    if base_url:
        d["verify_url"] = verify_url(base_url, row["qr_token"])
        d["qr_png"] = f"/api/policies/{row['id']}/qr.png"
        d["certificate_png"] = f"/api/policies/{row['id']}/certificate.png"
    return d


def for_booking(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM policy WHERE booking_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (booking_id,)
    ).fetchone()


def get(conn: sqlite3.Connection, pid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM policy WHERE id = ?", (pid,)).fetchone()


def quote_booking(conn: sqlite3.Connection, booking_id: int) -> pricing.Quote:
    b = conn.execute(
        "SELECT b.*, r.band FROM booking b LEFT JOIN risk_score r ON r.booking_id = b.id WHERE b.id = ?", (booking_id,)
    ).fetchone()
    if b is None:
        raise KeyError(booking_id)
    if b["band"] is None:
        q = pricing.Quote(eligible=False, reason="Not screened yet. Every booking is scored before cover is offered.")
        return q
    q = pricing.quote_for_row(b, b["band"])
    if q.eligible and b["status"] in ("on_hold", "rejected"):
        q.eligible = False
        q.reason = f"The booking is {b['status'].replace('_', ' ')} after review, so cover is not offered."
    return q


def issue(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row:
    """Issue cover for an eligible booking. Issuing twice returns the same policy."""
    existing = for_booking(conn, booking_id)
    if existing:
        return existing
    q = quote_booking(conn, booking_id)
    if not q.eligible:
        raise CoverError(q.reason or "Not eligible for cover.")
    token = secrets.token_urlsafe(24)  # 192 bits, unguessable
    cur = conn.execute(
        "INSERT INTO policy (booking_id, sum_insured_usd, premium_usd, status, qr_token, issued_at) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (booking_id, q.sum_insured_usd, q.premium_usd, token, db.iso(db.utcnow())),
    )
    conn.commit()
    return get(conn, int(cur.lastrowid))


def qr_png(url: str) -> bytes:
    return documents.png_bytes(documents.qr_image(url, box_size=10))


def certificate_png(conn: sqlite3.Connection, pid: int, base_url: str) -> bytes:
    p = get(conn, pid)
    if p is None:
        raise KeyError(pid)
    b = conn.execute(
        "SELECT b.*, s.name AS shipper_name FROM booking b JOIN shipper s ON s.id = b.shipper_id WHERE b.id = ?",
        (p["booking_id"],),
    ).fetchone()
    url = verify_url(base_url, p["qr_token"])
    img = documents.draw_certificate(
        "Certificate of cargo cover",
        policy_no(p["id"]),
        awbmod.fmt(b["awb"]),
        b["shipper_name"],
        f"{b['origin']} to {b['destination']}",
        b["flight_date"],
        b["commodity_text"],
        p["sum_insured_usd"],
        p["premium_usd"],
        p["issued_at"][:10],
        qr_data=url,
        verify_hint="Or send this certificate to the\nCargoShield chat to check it.",
    )
    return documents.png_bytes(img)


def band_of(conn: sqlite3.Connection, booking_id: int) -> str | None:
    r = risk.explain(conn, booking_id)
    return r["band"] if r else None
