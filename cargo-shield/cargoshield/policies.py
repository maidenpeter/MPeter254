"""Issue cover and produce its QR code and certificate (Milestone 4)."""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import timedelta
from dataclasses import dataclass
from typing import Any

from . import awb as awbmod
from . import db, documents, pricing, risk

_POLICY_NO_RE = re.compile(r"\bCSP[-\s]?(\d{5,8})\b", re.IGNORECASE)
_LINK_RE = re.compile(r"(?P<prefix>\S*?)/verify/(?P<kind>policy|awb)/(?P<value>[A-Za-z0-9_\-]{6,64})")
_AUTHORITY_RE = re.compile(r"[A-Za-z0-9.\-]+(:[0-9]{1,5})?")


class CoverError(Exception):
    """The booking can't be covered (HIGH risk, under review, no top-up needed...)."""


def policy_no(pid: int) -> str:
    return f"CSP-{pid:07d}"


def parse_policy_no(text: str) -> int | None:
    m = _POLICY_NO_RE.search(text or "")
    return int(m.group(1)) if m else None


def verify_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/verify/policy/{token}"


def find_link(text: str) -> "VerifyLink | None":
    """Find a CargoShield verification link (/verify/policy/... or /verify/awb/...) in text.

    The host is read strictly. Anything that could make a browser go somewhere
    other than the host we read (userinfo '@', backslashes, odd characters, a
    missing scheme) marks the link as not well formed, and callers treat that as
    untrusted.
    """
    m = _LINK_RE.search(text or "")
    if not m:
        return None
    prefix = m.group("prefix") or ""
    scheme_m = re.match(r"^(https?)://", prefix, re.IGNORECASE)
    if not scheme_m:
        return VerifyLink(m.group("kind"), m.group("value"), None, well_formed=False, has_prefix=bool(prefix))
    authority = prefix[scheme_m.end():]
    ok = bool(authority) and bool(_AUTHORITY_RE.fullmatch(authority))
    host = authority.rsplit(":", 1)[0].lower() if ok else (authority or None)
    return VerifyLink(m.group("kind"), m.group("value"), host, well_formed=ok, has_prefix=True)


@dataclass(frozen=True)
class VerifyLink:
    kind: str  # "policy" | "awb"
    value: str
    host: str | None
    well_formed: bool
    has_prefix: bool

    def trusted(self, hosts: set[str]) -> bool:
        return self.well_formed and self.host is not None and self.host in hosts


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
    departure = db.parse_dt(b["booked_at"]) + timedelta(hours=b["lead_time_hours"])
    if q.eligible and departure <= db.utcnow():
        # Cover has to be bought before the risk starts (no insuring a known outcome).
        q.eligible = False
        q.reason = (f"This flight left on {departure:%d %b %Y}. Cover has to be bought before departure, "
                    "so it is no longer offered.")
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
