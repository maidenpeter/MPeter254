"""Milestone 5: verification by AWB number, photo and QR."""

import pytest

from cargoshield import awb, config, db, fakes, messaging, ocr, policies, verify

needs_ocr = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")


def _log_count(conn):
    return conn.execute("SELECT COUNT(*) FROM verification_log").fetchone()[0]


def test_genuine_awb_returns_flight_facts_and_no_personal_data(conn):
    row = conn.execute("SELECT b.awb, s.name, s.phone, s.email FROM booking b JOIN shipper s ON s.id = b.shipper_id "
                       "WHERE b.status = 'accepted' LIMIT 1").fetchone()
    before = _log_count(conn)
    res = verify.verify_awb(conn, awb.fmt(row["awb"]))
    assert res.result == verify.GENUINE
    assert set(res.details) == {"awb", "origin", "destination", "flight_date", "pieces", "weight_kg", "carriage_status"}
    blob = str(res.to_dict())
    for secret in (row["name"], row["phone"], row["email"]):
        assert secret not in blob
    assert _log_count(conn) == before + 1


def test_flags_stay_internal(conn):
    high = db.get_meta(conn, "demo")["hero_high_awb"]
    res = verify.check_awb(conn, high)
    assert res.result == verify.GENUINE
    text = str(res.to_dict()).lower()
    for word in ("risk", "flag", "fraud", "review", "suspicious", "high"):
        assert word not in text, word


def test_not_found_and_invalid_format(conn):
    serial = fakes.FAKES[0].serial7
    assert verify.check_awb(conn, awb.make("999", serial)).result == verify.NOT_FOUND
    assert verify.check_awb(conn, "999-1234").result == verify.INVALID
    assert verify.check_awb(conn, "999-12345676").result == verify.INVALID  # bad check digit, typed
    assert verify.check_awb(conn, "999-12345676", from_document=True).result == verify.SUSPICIOUS
    other_airline = awb.make("176", "1234567")
    assert verify.check_awb(conn, other_airline).result == verify.OTHER_AIRLINE


def test_every_check_is_logged(conn):
    before = _log_count(conn)
    verify.verify_awb(conn, "nonsense")
    verify.verify_policy(conn, "no-such-token-000000000000")
    assert _log_count(conn) == before + 2
    last = conn.execute("SELECT * FROM verification_log ORDER BY id DESC LIMIT 1").fetchone()
    assert last["result"] == verify.NOT_FOUND and last["input_type"] == "qr"


def test_policy_token_verification(conn):
    clean = db.get_meta(conn, "demo")["hero_clean_booking_id"]
    p = policies.issue(conn, clean)
    res = verify.verify_policy(conn, p["qr_token"])
    assert res.result == verify.GENUINE and res.details["status"] == "active"
    assert verify.check_policy_token(conn, p["qr_token"][:-2] + "zz").result == verify.NOT_FOUND


needs_qr = pytest.mark.skipif(not ocr.qr_available(), reason="zxing-cpp not installed")


@needs_ocr
@pytest.mark.parametrize("spec", [
    pytest.param(f, marks=needs_qr if f.filename == "fake_cert_lookalike_qr.png" else (), id=f.filename)
    for f in fakes.FAKES
])
def test_every_fake_document_is_caught(conn, spec):
    data = (config.fakes_dir() / spec.filename).read_bytes()
    res = verify.verify_image(conn, data, channel="test", trusted_hosts={"localhost"})
    assert res.result == spec.expected, res.message


@needs_ocr
def test_genuine_sample_document_passes(conn):
    meta = db.get_meta(conn, "demo")
    data = (config.samples_dir() / f"genuine_awb_{meta['hero_clean_awb']}.png").read_bytes()
    res = verify.verify_image(conn, data, channel="test")
    assert res.result == verify.GENUINE


def test_issued_certificate_qr_scans_genuine(conn):
    if not ocr.qr_available():
        pytest.skip("zxing-cpp not installed")
    clean = db.get_meta(conn, "demo")["hero_clean_booking_id"]
    p = policies.issue(conn, clean)
    png = policies.certificate_png(conn, p["id"], "http://localhost:8000")
    res = verify.verify_image(conn, png, trusted_hosts={"localhost"})
    assert res.result == verify.GENUINE and res.kind == "policy"
    # A certificate whose QR points at some other host is a look-alike, whatever token it carries.
    fake = policies.certificate_png(conn, p["id"], "http://cargoshield-verify.example")
    assert verify.check_image(conn, fake, trusted_hosts={"localhost"}).result == verify.SUSPICIOUS


def test_unreadable_image_asks_for_the_number(conn):
    res = verify.verify_image(conn, b"not an image at all")
    assert res.result == verify.UNREADABLE and "type" in res.message.lower()


def test_ocr_missing_falls_back_to_manual_entry(conn, monkeypatch):
    monkeypatch.setattr(ocr, "read_text", lambda img: None)
    monkeypatch.setattr(ocr, "read_qr", lambda img: [])
    data = (config.fakes_dir() / fakes.FAKES[0].filename).read_bytes()
    res = verify.verify_image(conn, data)
    assert res.result == verify.UNREADABLE
    out = messaging.handle_message(conn, messaging.IncomingMessage("whatsapp-sim", "u1", image=data))
    assert "type" in out.text.lower()
