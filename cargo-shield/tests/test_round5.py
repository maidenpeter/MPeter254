"""Regression tests for the fifth review round."""

import json

import pytest

from cargoshield import awb, commodity, db, documents, ocr, policies, verify
from cargoshield.verify import _SCAM_RE

needs_ocr = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")


def _hero_clean(conn):
    return conn.execute("SELECT * FROM booking WHERE id = ?", (db.get_meta(conn, "demo")["hero_clean_booking_id"],)).fetchone()


def test_a_refusal_reaches_the_shippers_own_open_bookings(client, conn):
    it = client.get("/api/review-queue").json()["items"][-1]
    sid = conn.execute("SELECT shipper_id FROM booking WHERE id = ?", (it["id"],)).fetchone()[0]
    nb = client.post("/api/bookings", json={
        "shipper_id": sid, "origin": "NBO", "destination": "JNB", "commodity_text": "Kitchenware", "pieces": 5,
        "weight_kg": 100, "volume_m3": 0.5, "declared_value_usd": 2000, "payment_method": "bank_transfer",
        "lead_time_hours": 48}).json()
    client.post(f"/api/review/{it['id']}", json={"decision": "reject", "note": "Refused"})
    c2 = db.connect()
    hits = {h["id"] for h in json.loads(c2.execute("SELECT rule_hits_json FROM risk_score WHERE booking_id = ?",
                                                    (nb["booking"]["id"],)).fetchone()[0])}
    c2.close()
    assert "shipper_previously_rejected" in hits


def test_chunked_uploads_are_capped(client):
    def body():
        yield b'--zz\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
        for _ in range(12):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--zz--\r\n"

    r = client.post("/verify/image", content=body(), headers={"content-type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413


@pytest.mark.parametrize("text", ["Clearance charges payable by consignee", "Demurrage charges due",
                                  "Payable to the order of the shipper"])
def test_who_pays_lines_are_not_scams(text):
    assert not _SCAM_RE.search(text)


@pytest.mark.parametrize("text,expected", [("Cash registers", "general_cargo"), ("Jewellery boxes", "general_cargo"),
                                           ("Phone cases", "general_cargo"), ("Gold-plated cutlery", "general_cargo"),
                                           ("Jewellery", "valuables"), ("Bank notes", "valuables")])
def test_accessories_are_not_high_value(text, expected):
    assert commodity.classify(text) == expected


def test_typed_numbers_that_arent_ours_are_not_logged_raw(conn):
    verify.verify_awb(conn, "080 3123 4567", channel="test")  # an 11-digit phone number
    verify.verify_awb(conn, "138 1234 5678", channel="test")
    rows = [r[0] for r in conn.execute("SELECT input_value FROM verification_log ORDER BY id DESC LIMIT 2")]
    assert all("3123" not in v and "1234" not in v for v in rows)


@needs_ocr
def test_a_certificate_with_a_raised_amount_is_suspicious(conn):
    b = _hero_clean(conn)
    p = policies.issue(conn, b["id"])
    img = documents.draw_certificate("Certificate of cargo cover", policies.policy_no(p["id"]), awb.fmt(b["awb"]),
                                     "X", "NBO to LUN", "09 Nov 2026", "Pharmaceuticals", 900000, 300, "05 Nov 2026")
    assert verify.check_image(conn, documents.png_bytes(img)).result == verify.SUSPICIOUS


@needs_ocr
def test_a_cover_note_is_treated_like_a_certificate(conn):
    b = _hero_clean(conn)
    policies.issue(conn, b["id"])
    img = documents.draw_certificate("Cover note", "", awb.fmt(b["awb"]), "X", "NBO to LUN", "09 Nov 2026",
                                     "Pharmaceuticals", 50000, 300, "05 Nov 2026", issuer="Cargo insurance")
    assert verify.check_image(conn, documents.png_bytes(img)).result != verify.GENUINE


def test_heic_photos_get_a_plain_answer(conn):
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 100
    res = verify.check_image(conn, heic)
    assert res.result == verify.UNREADABLE and "JPEG" in res.message and "Error" not in res.message


def test_online_limit_uses_the_priced_sum_insured():
    import inspect

    src = inspect.getsource(policies.referral_reasons)
    assert "Decimal" in src and "float(b[" not in src


def test_stats_carry_the_seed_time(client):
    assert client.get("/api/stats").json()["seeded_at"]
