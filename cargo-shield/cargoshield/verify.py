"""Verify (Milestone 5): is this air waybill or cover certificate real?

Answers are written for the public. They never include shipper personal
data, and they never say a booking or a person is under suspicion. Internal
risk flags stay internal.

Results:
    GENUINE         the airline issued it (AWB) or the cover is real and active (policy)
    NOT FOUND       no such booking or policy
    INVALID FORMAT  typed input that isn't a well-formed AWB number
    SUSPICIOUS      a document whose number could never have been issued, whose
                    details don't match the record, or whose QR points elsewhere
    NOT ACTIVE      the policy exists but is no longer active
    UNREADABLE      the photo couldn't be read; ask for the number
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable

from . import awb as awbmod
from . import config, db, ocr, policies

GENUINE = "GENUINE"
NOT_FOUND = "NOT FOUND"
INVALID = "INVALID FORMAT"
SUSPICIOUS = "SUSPICIOUS"
NOT_ACTIVE = "NOT ACTIVE"
UNREADABLE = "UNREADABLE"

FAKE_RESULTS = (NOT_FOUND, SUSPICIOUS)

DO_NOT_PAY = ("Do not pay anyone, release goods or hand over money on the strength of this document. "
              "Call {airline} cargo on the number published on our official website.")


@dataclass
class VerifyResult:
    result: str
    kind: str  # "awb" | "policy" | "unknown"
    message: str
    input_value: str | None = None
    details: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _log(conn: sqlite3.Connection, channel: str, input_type: str, value: str | None, result: str) -> None:
    conn.execute(
        "INSERT INTO verification_log (channel, input_type, input_value, result, checked_at) VALUES (?, ?, ?, ?, ?)",
        (channel, input_type, value, result, db.iso(db.utcnow())),
    )
    conn.commit()


def _nice_date(iso_date: str) -> str:
    try:
        return date.fromisoformat(iso_date).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return iso_date


def _carriage_status(status: str) -> str:
    # Public wording. It never says "under review" or "flagged".
    return {"accepted": "Confirmed", "new": "Awaiting confirmation", "in_review": "Awaiting confirmation",
            "on_hold": "Awaiting confirmation", "rejected": "Not active"}.get(status, "Awaiting confirmation")


def _airline() -> str:
    return config.airline_name()


# ------------------------------------------------------------------ AWB


def _lookup_awb(conn: sqlite3.Connection, number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, awb, origin, destination, flight_date, pieces, weight_kg, status FROM booking WHERE awb = ?",
        (number,),
    ).fetchone()


def _awb_details(row: sqlite3.Row) -> dict[str, Any]:
    # Deliberately no shipper, consignee, contact, value or commodity.
    return {
        "awb": awbmod.fmt(row["awb"]),
        "origin": row["origin"],
        "destination": row["destination"],
        "flight_date": row["flight_date"],
        "pieces": row["pieces"],
        "weight_kg": row["weight_kg"],
        "carriage_status": _carriage_status(row["status"]),
    }


def check_awb(conn: sqlite3.Connection, text: str, from_document: bool = False) -> VerifyResult:
    """The check itself, without logging."""
    airline = _airline()
    c = awbmod.validate(text)
    if c.status == "bad_format":
        return VerifyResult(INVALID, "awb",
                            "That doesn't look like an air waybill number. It should be 3 digits, a dash, "
                            "then 8 digits, for example 999-12345675.", text.strip()[:40])
    shown = awbmod.fmt(c.awb)
    if c.status == "bad_check_digit":
        if from_document:
            return VerifyResult(SUSPICIOUS, "awb",
                                f"The number on this document, {shown}, fails the airline check-digit test, "
                                f"so {airline} could never have issued it. Treat the document as forged. "
                                + DO_NOT_PAY.format(airline=airline), shown)
        return VerifyResult(INVALID, "awb",
                            f"{shown} fails the air waybill check-digit test. Check the number for typing "
                            "mistakes. If you copied it from a document, treat that document as suspicious.", shown)
    if c.prefix != config.awb_prefix():
        return VerifyResult(NOT_FOUND, "awb",
                            f"{shown} is not a {airline} air waybill (prefix {c.prefix}). "
                            "Check it with the airline that issued it.", shown)
    row = _lookup_awb(conn, c.awb)
    if row is None:
        return VerifyResult(NOT_FOUND, "awb",
                            f"{airline} has no booking with air waybill {shown}. "
                            + DO_NOT_PAY.format(airline=airline), shown)
    d = _awb_details(row)
    return VerifyResult(GENUINE, "awb",
                        f"{airline} issued air waybill {shown}: {d['origin']} to {d['destination']}, flight date "
                        f"{_nice_date(d['flight_date'])}, {d['pieces']} pieces, {d['weight_kg']:,.0f} kg. "
                        f"Status: {d['carriage_status']}.", shown, d)


def verify_awb(conn: sqlite3.Connection, text: str, channel: str = "api", input_type: str = "text",
               from_document: bool = False) -> VerifyResult:
    res = check_awb(conn, text, from_document=from_document)
    _log(conn, channel, input_type, res.input_value, res.result)
    return res


# ------------------------------------------------------------------ policy


def _policy_details(conn: sqlite3.Connection, p: sqlite3.Row) -> dict[str, Any]:
    b = conn.execute("SELECT awb, origin, destination, flight_date FROM booking WHERE id = ?",
                     (p["booking_id"],)).fetchone()
    return {
        "policy_no": policies.policy_no(p["id"]),
        "status": p["status"],
        "awb": awbmod.fmt(b["awb"]),
        "route": f"{b['origin']} to {b['destination']}",
        "flight_date": b["flight_date"],
        "sum_insured_usd": p["sum_insured_usd"],
        "issued_at": p["issued_at"],
    }


def _policy_result(conn: sqlite3.Connection, p: sqlite3.Row | None, shown: str) -> VerifyResult:
    if p is None:
        return VerifyResult(NOT_FOUND, "policy",
                            "No CargoShield cover matches this certificate. Do not rely on it, and do not pay "
                            "anyone because of it.", shown)
    d = _policy_details(conn, p)
    if p["status"] != "active":
        return VerifyResult(NOT_ACTIVE, "policy", f"Cover {d['policy_no']} exists but is no longer active.", shown, d)
    return VerifyResult(GENUINE, "policy",
                        f"Cover {d['policy_no']} is real and active: air waybill {d['awb']}, {d['route']}, "
                        f"flight date {_nice_date(d['flight_date'])}, sum insured USD {d['sum_insured_usd']:,.2f}.",
                        shown, d)


def check_policy_token(conn: sqlite3.Connection, token: str) -> VerifyResult:
    token = (token or "").strip()
    p = conn.execute("SELECT * FROM policy WHERE qr_token = ?", (token,)).fetchone() if token else None
    return _policy_result(conn, p, f"token {token[:6]}…" if token else "")


def verify_policy(conn: sqlite3.Connection, token: str, channel: str = "api", input_type: str = "qr") -> VerifyResult:
    res = check_policy_token(conn, token)
    _log(conn, channel, input_type, res.input_value, res.result)
    return res


def check_policy_number(conn: sqlite3.Connection, pid: int, awb_on_document: str | None = None) -> VerifyResult:
    p = policies.get(conn, pid)
    res = _policy_result(conn, p, policies.policy_no(pid))
    if res.result == GENUINE and awb_on_document and res.details["awb"].replace("-", "") != awb_on_document:
        return VerifyResult(SUSPICIOUS, "policy",
                            f"Cover {policies.policy_no(pid)} is real but belongs to a different air waybill "
                            f"than the one on this certificate ({awbmod.fmt(awb_on_document)}). Someone may have "
                            "copied a genuine policy number onto another document. Do not pay on the strength of it.",
                            policies.policy_no(pid))
    return res


# ------------------------------------------------------------------ images

_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b")


def _known_airports() -> set[str]:
    from .seed import load_routes

    return {r.origin for r in load_routes()} | {r.destination for r in load_routes()}


def _route_mismatch(text: str, row: sqlite3.Row) -> bool:
    codes = set(_AIRPORT_RE.findall(text)) & _known_airports()
    if len(codes) < 2:
        return False  # not enough on the page to compare
    return not {row["origin"], row["destination"]} <= codes


def _qr_check(conn: sqlite3.Connection, payload: str, trusted_hosts: Iterable[str] | None) -> VerifyResult | None:
    token, host = policies.token_from_url(payload)
    if token:
        if trusted_hosts is not None and host and host.lower() not in {h.lower() for h in trusted_hosts}:
            return VerifyResult(SUSPICIOUS, "policy",
                                f"The QR code points to {host}, which is not the CargoShield verification service. "
                                "Don't follow it and don't pay on the strength of this document.", host)
        return check_policy_token(conn, token)
    numbers = awbmod.find_in_text(payload)
    if numbers:
        return check_awb(conn, numbers[0], from_document=True)
    if "://" in payload:
        from urllib.parse import urlparse

        host = urlparse(payload).hostname or payload[:40]
        return VerifyResult(SUSPICIOUS, "policy",
                            f"The QR code points to {host}, which is not the CargoShield verification service. "
                            "Don't follow it and don't pay on the strength of this document.", host)
    return None


def check_image(conn: sqlite3.Connection, data: bytes, trusted_hosts: Iterable[str] | None = None) -> VerifyResult:
    try:
        img = ocr.load_image(data)
    except ocr.BadImage as e:
        return VerifyResult(UNREADABLE, "unknown", f"I couldn't open that file ({e}). Please send a photo "
                            "or type the air waybill number.")

    for payload in ocr.read_qr(img):
        res = _qr_check(conn, payload, trusted_hosts)
        if res is not None:
            return res

    text = ocr.read_text(img)
    if text is None:
        return VerifyResult(UNREADABLE, "unknown",
                            "Photo reading isn't switched on here. Please type the air waybill number, "
                            "for example 999-12345675.")
    is_certificate = bool(re.search(r"certificate|insurance|policy number", text, re.IGNORECASE))
    numbers = awbmod.find_in_text(text, fix_ocr=True)
    if not numbers:
        return VerifyResult(UNREADABLE, "unknown",
                            "I couldn't find an air waybill number in that photo. Please type the number, "
                            "for example 999-12345675.")

    checks = [(n, awbmod.validate(n)) for n in numbers]
    valid = [n for n, c in checks if c.ok]
    if not valid:
        res = check_awb(conn, numbers[0], from_document=True)
        if is_certificate:
            res.kind = "policy"
        return res

    found = [n for n in valid if _lookup_awb(conn, n) is not None]
    number = found[0] if found else valid[0]

    if is_certificate:
        if not found:
            return VerifyResult(NOT_FOUND, "policy",
                                f"This certificate quotes air waybill {awbmod.fmt(number)}, but {_airline()} has no "
                                "such booking, so the certificate cannot be genuine. Do not pay on the strength of it.",
                                awbmod.fmt(number))
        pid = policies.parse_policy_no(text)
        if pid is not None:
            return check_policy_number(conn, pid, number)
        row = _lookup_awb(conn, number)
        p = policies.for_booking(conn, row["id"])
        return _policy_result(conn, p, awbmod.fmt(number))

    res = check_awb(conn, number, from_document=True)
    if res.result == GENUINE:
        row = _lookup_awb(conn, number)
        if _route_mismatch(text, row):
            return VerifyResult(SUSPICIOUS, "awb",
                                f"Air waybill {awbmod.fmt(number)} is real, but the airports on this document don't "
                                f"match the booking ({row['origin']} to {row['destination']}). The paperwork may have "
                                "been altered. " + DO_NOT_PAY.format(airline=_airline()), awbmod.fmt(number))
    return res


def verify_image(conn: sqlite3.Connection, data: bytes, channel: str = "api",
                 trusted_hosts: Iterable[str] | None = None) -> VerifyResult:
    res = check_image(conn, data, trusted_hosts)
    _log(conn, channel, "image", res.input_value or "unreadable", res.result)
    return res
