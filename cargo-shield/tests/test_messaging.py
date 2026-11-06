"""Chat handling behind one function, plus the (unconnected) WhatsApp adapter."""

from cargoshield import awb, config, db, fakes, messaging, policies, verify, whatsapp

M = messaging.IncomingMessage


def test_greeting_returns_help(conn):
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text="hi"))
    assert out.result is None and "air waybill" in out.text


def test_awb_in_free_text(conn):
    high = db.get_meta(conn, "demo")["hero_high_awb"]
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text=f"is {awb.fmt(high)} real?"))
    assert out.result == verify.GENUINE


def test_fake_number_warns_not_to_pay(conn):
    number = awb.fmt(awb.make(config.awb_prefix(), fakes.FAKES[0].serial7))
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text=number))
    assert out.result == verify.NOT_FOUND and "do not pay" in out.text.lower()


def test_policy_link_in_text(conn):
    clean = db.get_meta(conn, "demo")["hero_clean_booking_id"]
    p = policies.issue(conn, clean)
    url = policies.verify_url("http://localhost:8000", p["qr_token"])
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text=url))
    assert out.result == verify.GENUINE and out.kind == "policy"


def test_unrecognised_text(conn):
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text="where is my parcel"))
    assert out.result is None and "didn't catch" in out.text


def test_whatsapp_adapter_round_trip(conn):
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [
            {"from": "254000000000", "id": "m1", "type": "text", "text": {"body": awb.example(config.awb_prefix())}},
            {"from": "254000000000", "id": "m2", "type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg"}},
        ]}}]}]}
    msgs = whatsapp.parse_webhook(payload)
    assert [m[1] for m in msgs] == [None, "MEDIA1"]
    reply = messaging.handle_message(conn, msgs[0][0])
    body = whatsapp.build_text_reply("254000000000", reply)
    assert body["messaging_product"] == "whatsapp" and body["text"]["body"] == reply.text


def test_help_text_example_follows_the_configured_prefix():
    example = awb.example(config.awb_prefix())
    assert example in messaging.help_text()
    assert awb.validate(example).ok and example.startswith(config.awb_prefix() + "-")
