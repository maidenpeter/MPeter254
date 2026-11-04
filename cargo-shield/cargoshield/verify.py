"""Verify (Milestone 5): is this air waybill or cover certificate real?

Answers are written for the public. They never include shipper personal
data, and they never hint that a booking is under review or flagged. A booking
waiting for a reviewer reads exactly like any other booked shipment. Only a
cancelled (rejected) booking reads differently, as NOT ACTIVE, which is what
the airline would tell anyone who asked anyway.

Results:
    GENUINE         the airline issued it (AWB) or the cover is real and active (policy)
    NOT FOUND       no such booking or policy
    INVALID FORMAT  typed input that isn't a well-formed AWB number
    SUSPICIOUS      a document whose number could never have been issued, whose
                    details don't match the record, or whose QR points elsewhere
    NOT ACTIVE      the booking or policy exists but is no longer active
    UNREADABLE      the photo couldn't be read; ask for the number
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable
from urllib.parse import urlparse

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
CHECK_MATCH = "Check that the air waybill number and sum insured match your document."


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


def nice_date(iso_date: str) -> str:
    try:
        return date.fromisoformat(iso_date[:10]).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return iso_date


_nice_date = nice_date  # backwards-compatible name


def _airline() -> str:
    return config.airline_name()


def default_trusted_hosts() -> set[str]:
    """Hosts a genuine CargoShield verification link may point to."""
    hosts = {"localhost", "127.0.0.1"}
    configured = str(config.settings().get("server", {}).get("public_base_url") or "auto")
    if configured != "auto" and urlparse(configured).hostname:
        hosts.add(urlparse(configured).hostname.lower())
    return hosts


def _trusted(hosts: Iterable[str] | None) -> set[str]:
    return default_trusted_hosts() | {h.lower() for h in (hosts or ())}


def _foreign_link(where: str | None, kind: str = "policy") -> VerifyResult:
    place = where or "an address that isn't written as a normal web link"
    return VerifyResult(SUSPICIOUS, kind,
                        f"This link points to {place}, which is not the CargoShield verification service. "
                        "Don't follow it and don't pay on the strength of this document.", (where or "link")[:60])


# ------------------------------------------------------------------ AWB


def _lookup_awb(conn: sqlite3.Connection, number: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, awb, origin, destination, flight_date, pieces, weight_kg, status FROM booking WHERE awb = ?",
        (number,),
    ).fetchone()


def _awb_details(row: sqlite3.Row) -> dict[str, Any]:
    # Deliberately no shipper, consignee, contact, value, commodity or review status.
    return {
        "awb": awbmod.fmt(row["awb"]),
        "origin": row["origin"],
        "destination": row["destination"],
        "flight_date": row["flight_date"],
        "pieces": row["pieces"],
        "weight_kg": row["weight_kg"],
        "carriage_status": "Booked",
    }


def _masked_input(text: str) -> str:
    """What to log for input that isn't an AWB: never the raw text (it may be a phone number)."""
    digits = sum(ch.isdigit() for ch in text or "")
    return f"unrecognised input ({digits} digits)"


def check_awb(conn: sqlite3.Connection, text: str, from_document: bool = False) -> VerifyResult:
    """The check itself, without logging."""
    airline = _airline()
    c = awbmod.validate(text)
    if c.status == "bad_format":
        return VerifyResult(INVALID, "awb",
                            "That doesn't look like an air waybill number. It should be 3 digits, a dash, "
                            "then 8 digits, for example 999-12345675.", _masked_input(text))
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
    if row["status"] == "rejected":
        return VerifyResult(NOT_ACTIVE, "awb",
                            f"{airline} issued air waybill {shown}, but the booking is not active and the goods "
                            f"will not fly on it. " + DO_NOT_PAY.format(airline=airline), shown,
                            {"awb": shown, "carriage_status": "Not active"})
    d = _awb_details(row)
    return VerifyResult(GENUINE, "awb",
                        f"{airline} issued air waybill {shown}: {d['origin']} to {d['destination']}, flight date "
                        f"{nice_date(d['flight_date'])}, {d['pieces']} pieces, {d['weight_kg']:,.0f} kg.", shown, d)


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
        return VerifyResult(NOT_ACTIVE, "policy", f"Cover {d['policy_no']} exists but is no longer active.", shown,
                            {"policy_no": d["policy_no"], "status": p["status"]})
    return VerifyResult(GENUINE, "policy",
                        f"Cover {d['policy_no']} is real and active: air waybill {d['awb']}, {d['route']}, "
                        f"flight date {nice_date(d['flight_date'])}, sum insured USD {d['sum_insured_usd']:,.2f}.",
                        shown, d)


def check_policy_token(conn: sqlite3.Connection, token: str) -> VerifyResult:
    token = (token or "").strip()
    p = conn.execute("SELECT * FROM policy WHERE qr_token = ?", (token,)).fetchone() if token else None
    return _policy_result(conn, p, f"token {token[:6]}…" if token else "")


def verify_policy(conn: sqlite3.Connection, token: str, channel: str = "api", input_type: str = "qr") -> VerifyResult:
    res = check_policy_token(conn, token)
    _log(conn, channel, input_type, res.input_value, res.result)
    return res


def check_policy_number(conn: sqlite3.Connection, pid: int, awb_on_document: str | None) -> VerifyResult:
    """Check cover by policy number. Policy numbers are sequential, so on their own
    they prove nothing and reveal nothing: the air waybill number has to come with it."""
    shown = policies.policy_no(pid)
    if not awb_on_document:
        return VerifyResult(UNREADABLE, "policy",
                            f"To check cover {shown}, send the policy number together with its air waybill number, "
                            "or scan the QR code on the certificate.", shown)
    p = policies.get(conn, pid)
    if p is None:
        return _policy_result(conn, None, shown)
    booking_awb = conn.execute("SELECT awb FROM booking WHERE id = ?", (p["booking_id"],)).fetchone()["awb"]
    if booking_awb != awb_on_document:
        return VerifyResult(SUSPICIOUS, "policy",
                            f"Cover {shown} does not belong to air waybill {awbmod.fmt(awb_on_document)}. Someone may "
                            "have copied a genuine policy number onto another document. Do not pay on the strength of it.",
                            shown)
    return _policy_result(conn, p, shown)


# ------------------------------------------------------------------ images

_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b")
_CERT_RE = re.compile(r"certificate", re.IGNORECASE)
_COVER_RE = re.compile(r"\b(cover|insurance|policy)\b", re.IGNORECASE)


def _known_airports() -> set[str]:
    from .seed import load_routes

    routes = load_routes()
    return {r.origin for r in routes} | {r.destination for r in routes}


def _route_mismatch(text: str, row: sqlite3.Row) -> bool:
    codes = set(_AIRPORT_RE.findall(text)) & _known_airports()
    if len(codes) < 2:
        return False  # not enough on the page to compare
    return not {row["origin"], row["destination"]} <= codes


def _is_certificate(text: str) -> bool:
    # An air waybill can mention insurance ("Amount of insurance: NIL"); only a page
    # that calls itself a certificate of cover/insurance is treated as one.
    return bool(_CERT_RE.search(text) and _COVER_RE.search(text))


def check_image(conn: sqlite3.Connection, data: bytes, trusted_hosts: Iterable[str] | None = None) -> VerifyResult:
    trusted = _trusted(trusted_hosts)
    try:
        img = ocr.load_image(data)
    except ocr.BadImage as e:
        return VerifyResult(UNREADABLE, "unknown", f"I couldn't open that file ({e}). Please send a photo "
                            "or type the air waybill number.")

    # 1. QR codes. A link must point to a trusted host; anything else is a look-alike.
    qr_token: str | None = None
    qr_numbers: list[str] = []
    for payload in ocr.read_qr(img):
        link = policies.find_link(payload)
        if link is not None:
            if not link.trusted(trusted):
                return _foreign_link(link.host)
            if link.kind == "policy":
                qr_token = link.value
            else:
                qr_numbers += awbmod.find_in_text(link.value)
        elif "://" in payload or payload.lower().startswith("www."):
            return _foreign_link(urlparse(payload).hostname if "://" in payload else payload[:40])
        else:
            qr_numbers += awbmod.find_in_text(payload)

    # 2. Printed text, used to cross-check whatever the QR says.
    text = ocr.read_text(img)
    printed = awbmod.find_in_text(text, fix_ocr=True) if text else []

    if qr_token:
        res = check_policy_token(conn, qr_token)
        if res.result != GENUINE:
            return res
        cover_awb = res.details["awb"].replace("-", "")
        printed_valid = [n for n in printed if awbmod.validate(n).ok]
        if printed_valid and cover_awb not in printed_valid:
            return VerifyResult(SUSPICIOUS, "policy",
                                f"The QR code belongs to genuine cover for a different shipment "
                                f"({res.details['awb']}) than the one printed on this certificate "
                                f"({awbmod.fmt(printed_valid[0])}). The certificate has been altered. "
                                "Do not pay on the strength of it.", res.input_value)
        res.message += " " + CHECK_MATCH
        if not printed_valid:
            res.warnings.append("The printed air waybill number could not be read to compare with the QR code.")
        return res

    if text is None and not qr_numbers:
        return VerifyResult(UNREADABLE, "unknown",
                            "Photo reading isn't switched on here. Please type the air waybill number, "
                            "for example 999-12345675.")
    text = text or ""
    numbers = list(dict.fromkeys(qr_numbers + printed))
    if not numbers:
        return VerifyResult(UNREADABLE, "unknown",
                            "I couldn't find an air waybill number in that photo. Please type the number, "
                            "for example 999-12345675.")

    is_certificate = _is_certificate(text)
    checks = [(n, awbmod.validate(n)) for n in numbers]
    valid = [n for n, c in checks if c.ok]
    if not valid:
        res = check_awb(conn, numbers[0], from_document=True)
        if is_certificate:
            res.kind = "policy"
        return res
    if qr_numbers and printed and qr_numbers[0] not in printed and awbmod.validate(qr_numbers[0]).ok:
        return VerifyResult(SUSPICIOUS, "awb",
                            f"The QR code ({awbmod.fmt(qr_numbers[0])}) and the printed air waybill number don't "
                            "match. " + DO_NOT_PAY.format(airline=_airline()), awbmod.fmt(qr_numbers[0]))

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
        res = _policy_result(conn, policies.for_booking(conn, row["id"]), awbmod.fmt(number))
        if res.result == GENUINE:
            res.message += " " + CHECK_MATCH
        return res

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
