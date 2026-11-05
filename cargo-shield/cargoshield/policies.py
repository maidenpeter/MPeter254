"""Issue cover and produce its QR code and certificate (Milestone 4)."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Any

from . import awb as awbmod
from . import commodity, config, db, documents, graph, pricing, risk

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
    if base_url:  # dashboard view: add the end of cover
        fd = None
        try:
            fd = row["flight_date"]
        except (IndexError, KeyError):
            pass
        if fd:
            ends = cover_ends(fd)
            d["cover_ends"] = ends.isoformat()
            d["cover_ended"] = ends < db.utcnow().date()
    if base_url:
        d["verify_url"] = verify_url(base_url, row["qr_token"])
        d["qr_png"] = f"/api/policies/{row['id']}/qr.png"
        d["certificate_png"] = f"/api/policies/{row['id']}/certificate.png"
    return d


def for_booking(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, b.flight_date FROM policy p JOIN booking b ON b.id = p.booking_id "
        "WHERE p.booking_id = ? AND p.status = 'active' ORDER BY p.id DESC LIMIT 1", (booking_id,)
    ).fetchone()


def get(conn: sqlite3.Connection, pid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, b.flight_date FROM policy p JOIN booking b ON b.id = p.booking_id WHERE p.id = ?", (pid,)
    ).fetchone()


def departure_of(row: Any) -> datetime:
    return db.parse_dt(row["booked_at"]) + timedelta(hours=row["lead_time_hours"])


def cover_ends(flight_date: str) -> date:
    days = int(config.settings()["cover"].get("cover_days_after_flight", 30))
    return date.fromisoformat(flight_date[:10]) + timedelta(days=days)


def referral_reasons(conn: sqlite3.Connection, b: Any, flagged: set[int] | None = None) -> list[str]:
    """Why cover for this booking needs an underwriter rather than a click."""
    c = config.settings()["cover"]
    out = []
    shipper = conn.execute("SELECT id, kyc_status FROM shipper WHERE id = ?", (b["shipper_id"],)).fetchone()
    if shipper["kyc_status"] != "verified":
        out.append("the shipper's KYC checks are not complete")
    if shipper["id"] in (flagged if flagged is not None else graph.flagged_shippers(conn)):
        out.append("the shipper is under internal review")
    if b["commodity_class"] in (c.get("refer_classes") or []):
        out.append(f"{commodity.label(b['commodity_class']).lower()} need specialist cover")
    hits = conn.execute("SELECT rule_hits_json FROM risk_score WHERE booking_id = ?", (b["id"],)).fetchone()
    fired = {h["id"] for h in json.loads(hits["rule_hits_json"])} if hits else set()
    if fired & set(c.get("refer_if_rules") or []):
        out.append("the risk reasons on this booking need a person to look at them")
    return out


def quote_booking(conn: sqlite3.Connection, booking_id: int, as_of: datetime | None = None,
                  flagged: set[int] | None = None) -> pricing.Quote:
    """The quote as the dashboard shows it. `as_of` is the moment of purchase (now by default)."""
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
    departure = departure_of(b)
    if q.eligible and departure <= (as_of or db.utcnow()):
        # Cover has to be bought before the risk starts (no insuring a known outcome).
        q.eligible = False
        q.bindable = False
        q.reason = (f"This flight left on {departure:%d %b %Y}. Cover has to be bought before departure, "
                    "so it is no longer offered.")
    if q.eligible:
        q.referral_reasons = referral_reasons(conn, b, flagged)
        q.bindable = not q.referral_reasons
    return q


def issue(conn: sqlite3.Connection, booking_id: int) -> sqlite3.Row:
    """Issue cover for an eligible booking. Issuing twice returns the same policy."""
    existing = for_booking(conn, booking_id)
    if existing:
        return existing
    q = quote_booking(conn, booking_id)
    if not q.eligible:
        raise CoverError(q.reason or "Not eligible for cover.")
    if not q.bindable:
        raise CoverError("Referred to an underwriter: " + "; ".join(q.referral_reasons) + ".")
    token = secrets.token_urlsafe(24)  # 192 bits, unguessable
    try:
        cur = conn.execute(
            "INSERT INTO policy (booking_id, sum_insured_usd, premium_usd, status, qr_token, issued_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (booking_id, q.sum_insured_usd, q.premium_usd, token, db.iso(db.utcnow())),
        )
        conn.commit()
    except sqlite3.IntegrityError:  # a second click raced the first: one active policy per booking
        conn.rollback()
        existing = for_booking(conn, booking_id)
        if existing:
            return existing
        raise
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
        f"{date.fromisoformat(b['flight_date']):%d %b %Y}",
        b["commodity_text"],
        p["sum_insured_usd"],
        p["premium_usd"],
        f"{date.fromisoformat(p['issued_at'][:10]):%d %b %Y}",
        value_of_goods_usd=b["declared_value_usd"],
        basis="In excess of carrier liability (Montreal Convention Art. 22(3)). No value declared for carriage.",
        valid_until=f"{cover_ends(b['flight_date']):%d %b %Y}",
        qr_data=url,
        verify_hint="Or send this certificate to the\nCargoShield chat to check it.",
    )
    return documents.png_bytes(img)


def band_of(conn: sqlite3.Connection, booking_id: int) -> str | None:
    r = risk.explain(conn, booking_id)
    return r["band"] if r else None
