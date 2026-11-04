"""WhatsApp Business Cloud API adapter. NOT CONNECTED in the demo.

The demo uses the chat simulator in the web UI. This module shows the only
code a live WhatsApp channel needs: turn the webhook payload into an
IncomingMessage, call messaging.handle_message(), and turn the reply into a
send-message request body. No network calls are made from here.

To go live (outside the demo):
  1. Register a WhatsApp Business number and app in Meta's developer console.
  2. Expose POST /webhooks/whatsapp, verify the hub.challenge handshake, and
     check the X-Hub-Signature-256 header against the app secret.
  3. For image messages, download the media by its id with the Graph API, then
     pass the bytes in IncomingMessage.image.
  4. POST the body from build_text_reply() to the Graph API messages endpoint.
Check Meta's current Cloud API documentation before building this: payload
shapes and API versions change, and this adapter has not been tested against
the live service.
"""

from __future__ import annotations

from typing import Any

from .messaging import IncomingMessage, OutgoingMessage


def parse_webhook(payload: dict[str, Any]) -> list[tuple[IncomingMessage, str | None]]:
    """Return (message, media_id) pairs from a Cloud API webhook payload.

    media_id is set for image messages; the caller downloads the bytes.
    """
    out: list[tuple[IncomingMessage, str | None]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for m in value.get("messages") or []:
                sender = m.get("from", "")
                mtype = m.get("type")
                if mtype == "text":
                    out.append((IncomingMessage("whatsapp", sender, text=(m.get("text") or {}).get("body")), None))
                elif mtype == "image":
                    img = m.get("image") or {}
                    out.append((IncomingMessage("whatsapp", sender, text=img.get("caption"),
                                                image_mime=img.get("mime_type")), img.get("id")))
    return out


def build_text_reply(to: str, reply: OutgoingMessage) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": reply.text[:4096]},
    }
