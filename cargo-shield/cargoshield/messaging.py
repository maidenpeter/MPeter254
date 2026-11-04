"""Channel-agnostic chat handling.

Every chat channel (the web simulator today, WhatsApp later) turns its
messages into an IncomingMessage and calls handle_message(). The reply comes
back as plain text plus structured fields. Nothing in here knows about HTTP
or WhatsApp, so wiring a real channel means writing an adapter only
(see whatsapp.py).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import awb as awbmod
from . import config, policies, verify

ICONS = {
    verify.GENUINE: "✅",
    verify.NOT_FOUND: "❌",
    verify.SUSPICIOUS: "⚠️",
    verify.INVALID: "✏️",
    verify.NOT_ACTIVE: "⚠️",
    verify.UNREADABLE: "📷",
}

_GREETING = re.compile(r"^\s*(hi|hello|hey|help|start|menu|habari|jambo|\?)\W*$", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\d[\d\s\-–.]{6,}\d")


@dataclass
class IncomingMessage:
    channel: str  # e.g. "whatsapp-sim", "whatsapp"
    sender: str  # opaque id; never stored with the verification log
    text: str | None = None
    image: bytes | None = None
    image_mime: str | None = None


@dataclass
class OutgoingMessage:
    text: str
    result: str | None = None
    kind: str | None = None
    details: dict[str, Any] | None = None
    quick_replies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def help_text() -> str:
    airline = config.airline_name()
    return (f"Hello. This is CargoShield for {airline}.\n"
            "Send me any of these and I'll tell you if it's real:\n"
            "• an air waybill number, like 999-12345675\n"
            "• a photo of the air waybill or insurance certificate\n"
            "• the link from the QR code on a CargoShield certificate")


def _format(res: verify.VerifyResult) -> OutgoingMessage:
    icon = ICONS.get(res.result, "")
    head = f"{icon} {res.result}".strip()
    lines = [head, res.message]
    if res.result == verify.GENUINE and res.kind == "awb":
        lines.append(f"Only pay through {config.airline_name()}'s official channels.")
    return OutgoingMessage("\n".join(lines), res.result, res.kind, res.details)


def handle_message(conn: sqlite3.Connection, msg: IncomingMessage,
                   trusted_hosts: Iterable[str] | None = None) -> OutgoingMessage:
    if msg.image:
        res = verify.verify_image(conn, msg.image, channel=msg.channel, trusted_hosts=trusted_hosts)
        out = _format(res)
        if res.result == verify.UNREADABLE:
            out.quick_replies = ["Type the AWB number"]
        return out

    text = (msg.text or "").strip()
    if not text or _GREETING.match(text):
        return OutgoingMessage(help_text())

    token, _host = policies.token_from_url(text)
    if token:
        return _format(verify.verify_policy(conn, token, channel=msg.channel, input_type="link"))

    pid = policies.parse_policy_no(text)
    if pid is not None:
        numbers = awbmod.find_in_text(text)
        res = verify.check_policy_number(conn, pid, numbers[0] if numbers else None)
        verify._log(conn, msg.channel, "text", res.input_value, res.result)
        return _format(res)

    numbers = awbmod.find_in_text(text)
    if numbers:
        return _format(verify.verify_awb(conn, numbers[0], channel=msg.channel, input_type="text"))

    attempt = _LONG_DIGITS.search(text)
    if attempt:
        return _format(verify.verify_awb(conn, attempt.group(0), channel=msg.channel, input_type="text"))

    return OutgoingMessage("I didn't catch an air waybill number there.\n\n" + help_text())
