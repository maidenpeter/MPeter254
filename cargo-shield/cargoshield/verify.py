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
    NOT ACTIVE      the booking was refused, or the cover was cancelled or has ended
    OTHER AIRLINE   a well-formed AWB with another airline's prefix
    UNREADABLE      the photo couldn't be read; ask for the number
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable
from urllib.parse import urlparse

from . import awb as awbmod
from . import config, db, ocr, policies

GENUINE = "GENUINE"
NOT_FOUND = "NOT FOUND"
INVALID = "INVALID FORMAT"
SUSPICIOUS = "SUSPICIOUS"
NOT_ACTIVE = "NOT ACTIVE"
OTHER_AIRLINE = "OTHER AIRLINE"
UNREADABLE = "UNREADABLE"

FAKE_RESULTS = (NOT_FOUND, SUSPICIOUS)
DOCUMENT_INPUTS = ("image", "qr", "link")  # what counts towards "fakes caught"

DO_NOT_PAY = ("Do not pay anyone, release goods or hand over money on the strength of this document. "
              "Call {airline} cargo on the number on {airline}'s official website.")
CHECK_MATCH = "Check that the air waybill number and sum insured match your certificate."
OFFICIAL_CHANNELS = "Only pay through {airline}'s official channels."

# Wording that appears on scam paperwork and never on a real air waybill: fees
# demanded by an "agent" before goods are released or loaded.
_SCAM_RE = re.compile(
    # "pay ... to the agent", "send the fee via our clearing agent"
    r"\b(pay|payment|paid|remit|send|deposit|settle|settled)\b[^.\n]{0,60}\b(to|via|through)\s+"
    r"(the\s+|our\s+|an?\s+|your\s+)?(\w+\s+)?agent"
    r"|\bpayable\s+to\b(?!\s+(the\s+)?order\s+of)"
    # "release on payment of", "cleared upon full payment"
    r"|\b(release|released|clearing|clearance|cleared)\s+(on|upon|after|against)\s+(full\s+)?payment"
    # "pay the storage charges ... to avoid auction / before release / before loading"
    r"|\bpay(ment)?\b[^.\n]{0,50}\b(fee|fees|charge|charges)\b[^.\n]{0,40}\b(before|to avoid|or)\b[^.\n]{0,20}"
    r"\b(release|loading|auction|seizure|forfeit)"
    # fee names with a demand next to them: "clearance charges must be paid", "pay the release fee"
    r"|\b(release|clearance|clearing|demurrage|forwarding)\s+(fee|fees|charges?)\b[^.\n]{0,60}"
    r"(\b(pay|remit|send|settle)\b|\b(payable|due)\s+(to|before|immediately|now)\b|\brequired\s+before\b)"
    r"|\b(pay|payable|remit|send|due|required|settle)\b[^.\n]{0,40}\b(release|clearance|clearing|demurrage)\s+(fee|fees|charges?)\b"
    r"|\bbalance\s+of\s+(usd|kes|\$)[^.\n]{0,40}\bdue\b"
    r"|\bto\s+avoid\s+(auction|seizure|forfeiture)"
    # payment channels an airline never prints on an air waybill
    r"|\bm-?pesa\b|\bpaybill\b|\btill\s+(no|number)\b|\blipa\s+na\b|\bwestern\s+union\b|\bmoneygram\b"
    r"|\bkindly\s+(remit|pay|send|deposit)\b",
    re.IGNORECASE,
)


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
        "SELECT id, awb, origin, destination, flight_date, pieces, weight_kg, status, booked_at, lead_time_hours "
        "FROM booking WHERE awb = ?",
        (number,),
    ).fetchone()


def _carriage_status(row: sqlite3.Row) -> str:
    # Public wording follows the flight only. A booking waiting for a reviewer reads
    # exactly like any other one, so the internal flag never shows.
    full = conn_row_departure(row)
    if row["status"] == "accepted" and full is not None and full <= db.utcnow():
        return "Flown"
    return "Booked"


def conn_row_departure(row: sqlite3.Row):
    try:
        return db.parse_dt(row["booked_at"]) + timedelta(hours=row["lead_time_hours"])
    except (IndexError, KeyError, TypeError):
        return None


def _weight(w: float) -> str:
    return f"{w:,.1f} kg" if w < 100 else f"{w:,.0f} kg"


def _pieces(n: int) -> str:
    return "1 piece" if n == 1 else f"{n} pieces"


def _awb_details(row: sqlite3.Row) -> dict[str, Any]:
    # Deliberately no shipper, consignee, contact, value, commodity or review status.
    return {
        "awb": awbmod.fmt(row["awb"]),
        "origin": row["origin"],
        "destination": row["destination"],
        "flight_date": row["flight_date"],
        "pieces": row["pieces"],
        "weight_kg": row["weight_kg"],
        "carriage_status": _carriage_status(row),
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
        # Typed by a person: it may be a phone number, so the log keeps no digits.
        return VerifyResult(INVALID, "awb",
                            f"{shown} fails the air waybill check-digit test. Check the number for typing "
                            "mistakes. If you copied it from a document, treat that document as suspicious.",
                            "11 digits, check digit fails")
    if c.prefix != config.awb_prefix():
        return VerifyResult(OTHER_AIRLINE, "awb",
                            f"{shown} is not a {airline} air waybill (its prefix {c.prefix} belongs to another "
                            "airline). Check it with the airline that issued it.",
                            shown if from_document else f"other airline, prefix {c.prefix}")
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
                        f"{nice_date(d['flight_date'])}, {_pieces(d['pieces'])}, {_weight(d['weight_kg'])}. "
                        + OFFICIAL_CHANNELS.format(airline=airline), shown, d)


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
    refused = conn.execute("SELECT status FROM booking WHERE id = ?", (p["booking_id"],)).fetchone()["status"] == "rejected"
    if p["status"] != "active" or refused:
        return VerifyResult(NOT_ACTIVE, "policy", f"Cover {d['policy_no']} exists but is no longer active.", shown,
                            {"policy_no": d["policy_no"], "status": p["status"]})
    ends = policies.cover_ends(d["flight_date"])
    if ends < db.utcnow().date():
        return VerifyResult(NOT_ACTIVE, "policy",
                            f"Cover {d['policy_no']} was real, but it ended on {ends:%d %b %Y}, after the shipment's "
                            "transit. It does not cover anything now.", shown,
                            {"policy_no": d["policy_no"], "status": "ended", "ended_on": ends.isoformat()})
    d["cover_ends"] = ends.isoformat()
    return VerifyResult(GENUINE, "policy",
                        f"Cover {d['policy_no']} is real and active: air waybill {d['awb']}, {d['route']}, "
                        f"flight date {nice_date(d['flight_date'])}, sum insured USD {d['sum_insured_usd']:,.2f}. "
                        + CHECK_MATCH, shown, d)


def check_policy_token(conn: sqlite3.Connection, token: str) -> VerifyResult:
    token = (token or "").strip()
    p = conn.execute("SELECT * FROM policy WHERE qr_token = ?", (token,)).fetchone() if token else None
    return _policy_result(conn, p, f"token {token[:6]}…" if token else "")


def verify_policy(conn: sqlite3.Connection, token: str, channel: str = "api", input_type: str = "qr") -> VerifyResult:
    res = check_policy_token(conn, token)
    if not (20 <= len((token or "").strip()) <= 64):
        input_type = "text"  # not shaped like an issued token: someone typing, not a scanned document
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
    res = _policy_result(conn, p, shown)
    if res.result == GENUINE:
        # Policy numbers are sequential, so they never unlock the amount covered.
        d = res.details
        res.details = {k: d[k] for k in ("policy_no", "status", "awb", "flight_date", "cover_ends") if k in d}
        res.message = (f"Cover {d['policy_no']} is real and active for air waybill {d['awb']}, flight date "
                       f"{nice_date(d['flight_date'])}. To see the amount covered, scan the QR code on the certificate.")
    return res


# ------------------------------------------------------------------ images

_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b")
_DOMAIN_RE = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")  # a bare web address in a QR code
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


def _fee_demand(number: str, what: str) -> VerifyResult:
    return VerifyResult(SUSPICIOUS, "policy" if what == "certificate" else "awb",
                        f"Air waybill {awbmod.fmt(number)} is real, but this {what} asks for money to be paid to "
                        f"someone before the goods move. {_airline()} never collects fees through agents or chat "
                        "messages. " + DO_NOT_PAY.format(airline=_airline()), awbmod.fmt(number))


def _ocr_slip(number: str, expected: str) -> bool:
    """True if `number` is `expected` with one digit misread (and so fails the check digit)."""
    if len(number) != len(expected) or awbmod.validate(number).ok:
        return False
    return sum(a != b for a, b in zip(number, expected)) == 1


def _is_certificate(text: str) -> bool:
    """Is this page about insurance cover (certificate, cover note, policy schedule)?

    An air waybill can mention insurance ("Amount of insurance: NIL"), so that word
    alone isn't enough; a policy number or a sum insured is."""
    if _CERT_RE.search(text) and _COVER_RE.search(text):
        return True
    return bool(_SUM_INSURED_RE.search(text) or policies.parse_policy_no(text) is not None
                or re.search(r"\b(cover\s+note|policy\s+schedule|schedule\s+of\s+insurance)\b", text, re.IGNORECASE))


_SUM_INSURED_RE = re.compile(r"sum\s+insured[^0-9\n]{0,12}([0-9][0-9,]*(?:\.[0-9]{2})?)", re.IGNORECASE)


def _printed_amount_differs(text: str, actual: float) -> bool:
    """True if the page prints a sum insured that isn't the real one (OCR slips aside)."""
    m = _SUM_INSURED_RE.search(text or "")
    if not m:
        return False
    try:
        printed = float(m.group(1).replace(",", ""))
    except ValueError:
        return False
    return abs(printed - actual) > max(1.0, 0.01 * actual)


_IMAGE_SLOTS = threading.BoundedSemaphore(2)  # OCR is heavy: two photos at a time


def check_image(conn: sqlite3.Connection, data: bytes, trusted_hosts: Iterable[str] | None = None) -> VerifyResult:
    if not _IMAGE_SLOTS.acquire(timeout=20):
        return VerifyResult(UNREADABLE, "unknown", "Busy checking other photos. Please send it again in a moment, "
                            "or type the air waybill number.")
    try:
        return _check_image(conn, data, trusted_hosts)
    finally:
        _IMAGE_SLOTS.release()


def _check_image(conn: sqlite3.Connection, data: bytes, trusted_hosts: Iterable[str] | None = None) -> VerifyResult:
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
        elif "://" in payload or payload.lower().startswith("www.") or _DOMAIN_RE.search(payload):
            m = _DOMAIN_RE.search(payload)
            return _foreign_link(urlparse(payload).hostname if "://" in payload else (m.group(0) if m else payload[:40]))
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
        on_page = list(dict.fromkeys(printed + qr_numbers))
        others = [n for n in on_page if n != cover_awb and not _ocr_slip(n, cover_awb)]
        if others:
            return VerifyResult(SUSPICIOUS, "policy",
                                f"The QR code belongs to genuine cover for a different shipment "
                                f"({res.details['awb']}) than the one printed on this certificate "
                                f"({awbmod.fmt(others[0])}). The certificate has been altered. "
                                "Do not pay on the strength of it.", res.input_value)
        if text and _SCAM_RE.search(text):
            return _fee_demand(cover_awb, "certificate")
        if not on_page:
            res.warnings.append("The air waybill number printed on the certificate could not be read, so compare "
                                "it with the one above yourself.")
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

    # Two different, well-formed AWB numbers of ours on one page (allowing a one-digit
    # OCR slip) means someone pasted a real number onto another document. Other
    # prefixes are left out: an 11-digit phone number can look like an AWB.
    ours = [n for n in valid if n[:3] == config.awb_prefix()]
    distinct = []
    for n in ours:
        if not any(n == m or sum(a != b for a, b in zip(n, m)) <= 1 for m in distinct):
            distinct.append(n)
    if len(distinct) > 1:
        return VerifyResult(SUSPICIOUS, "policy" if is_certificate else "awb",
                            f"This document carries more than one air waybill number ({awbmod.fmt(distinct[0])} and "
                            f"{awbmod.fmt(distinct[1])}). Genuine paperwork carries one. "
                            + DO_NOT_PAY.format(airline=_airline()), awbmod.fmt(distinct[0]))

    found = [n for n in valid if _lookup_awb(conn, n) is not None]
    number = found[0] if found else (ours[0] if ours else valid[0])

    if awbmod.validate(number).prefix != config.awb_prefix():
        # Another airline's number. A paper that still claims to be ours, or asks for
        # a fee, is a fake; otherwise send the person to that airline.
        kind = "policy" if is_certificate else "awb"
        if re.search(re.escape(_airline()), text, re.IGNORECASE):
            return VerifyResult(SUSPICIOUS, kind,
                                f"This document names {_airline()} but its number, {awbmod.fmt(number)}, belongs to "
                                "another airline. " + DO_NOT_PAY.format(airline=_airline()), awbmod.fmt(number))
        if _SCAM_RE.search(text):
            return VerifyResult(SUSPICIOUS, kind,
                                f"This is another airline's air waybill ({awbmod.fmt(number)}), and it asks for money to "
                                "be paid to someone before the goods move. Airlines don't collect fees that way. Check "
                                "with the airline that issued it before paying anything.", awbmod.fmt(number))
        res = check_awb(conn, number, from_document=True)
        res.kind = "policy" if is_certificate else "awb"
        if is_certificate:
            res.message = (f"This certificate is for {awbmod.fmt(number)}, another airline's air waybill. CargoShield "
                           f"can only check cover on {_airline()} shipments. Check it with the insurer named on it.")
        return res

    if is_certificate:
        if not found:
            return VerifyResult(NOT_FOUND, "policy",
                                f"This certificate quotes air waybill {awbmod.fmt(number)}, but {_airline()} has no "
                                "such booking, so the certificate cannot be genuine. Do not pay on the strength of it.",
                                awbmod.fmt(number))
        if _SCAM_RE.search(text):
            return _fee_demand(number, "certificate")
        pid = policies.parse_policy_no(text)
        if pid is not None:
            res = check_policy_number(conn, pid, number)
            p = policies.get(conn, pid) if res.result == GENUINE else None
            if p is not None and _printed_amount_differs(text, p["sum_insured_usd"]):
                # Says only that the paper is wrong, not what the real amount is.
                return VerifyResult(SUSPICIOUS, "policy",
                                    f"Cover {policies.policy_no(pid)} is real, but the sum insured printed on this "
                                    "certificate doesn't match it. The certificate has been altered. Scan the QR code "
                                    "on the original, and do not pay on the strength of this one.", res.input_value)
            return res
        # No QR and no policy number to go on: never confirm (or reveal) cover from an AWB alone.
        return VerifyResult(UNREADABLE, "policy",
                            f"Air waybill {awbmod.fmt(number)} is real, but I can only confirm the cover itself from "
                            "the QR code on the certificate, or from its policy number sent with the air waybill "
                            "number.", awbmod.fmt(number))

    res = check_awb(conn, number, from_document=True)
    if res.result == GENUINE:
        row = _lookup_awb(conn, number)
        if _SCAM_RE.search(text):
            return _fee_demand(number, "document")
        if _route_mismatch(text, row):
            return VerifyResult(SUSPICIOUS, "awb",
                                f"Air waybill {awbmod.fmt(number)} is real, but the airports on this document don't "
                                f"match the booking ({row['origin']} to {row['destination']}). The paperwork may have "
                                "been altered. " + DO_NOT_PAY.format(airline=_airline()), awbmod.fmt(number))
        res.message = (f"The number is real. Now check the paper against our record: {row['origin']} to "
                       f"{row['destination']}, flight date {nice_date(row['flight_date'])}, {_pieces(row['pieces'])}, "
                       f"{_weight(row['weight_kg'])}. If anything differs, the paper has been altered: do not pay. "
                       + OFFICIAL_CHANNELS.format(airline=_airline()))
    return res


def verify_image(conn: sqlite3.Connection, data: bytes, channel: str = "api",
                 trusted_hosts: Iterable[str] | None = None) -> VerifyResult:
    res = check_image(conn, data, trusted_hosts)
    _log(conn, channel, "image", res.input_value or "unreadable", res.result)
    return res
