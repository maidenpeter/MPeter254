"""Regression tests for the fourth review round."""

import json

import pytest

from cargoshield import awb, config, db, documents, messaging, ocr, policies, verify
from cargoshield.features import _hours_text
from cargoshield.verify import _SCAM_RE

needs_ocr = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")
needs_qr = pytest.mark.skipif(not ocr.qr_available(), reason="zxing-cpp not installed")
M = messaging.IncomingMessage


def _png(img):
    return documents.png_bytes(img)


def _hero_clean(conn):
    return conn.execute("SELECT * FROM booking WHERE id = ?", (db.get_meta(conn, "demo")["hero_clean_booking_id"],)).fetchone()


@pytest.mark.parametrize("text", [
    "Release on payment of USD 1,850 clearing fee to agent",
    "Kindly remit the clearance charges",
    "Clearance charges payable to Kito Logistics",
    "Pay via MPESA paybill 123456",
    "Release fee of USD 300 required before collection",
    "Send demurrage fees to our clearing agent",
    "Goods cleared upon full payment",
    "Pay storage charges to agent to avoid auction",
])
def test_scam_wording_is_caught(text):
    assert _SCAM_RE.search(text)


@pytest.mark.parametrize("text", [
    "Handling charge USD 45", "Storage charges apply after 48 hours", "Other charges due carrier",
    "Customs fee paid by consignee", "Clearance fee prepaid", "Charges collect", "Not negotiable",
    "Shipper certifies that the particulars on the face hereof are correct",
])
def test_ordinary_air_waybill_wording_is_not(text):
    assert not _SCAM_RE.search(text)


def test_eps_and_other_non_photo_formats_are_refused(conn):
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    assert verify.check_image(conn, eps).result == verify.UNREADABLE


@needs_ocr
def test_a_phone_number_is_not_a_second_air_waybill(conn):
    b = _hero_clean(conn)
    img = documents.draw_awb(awb.fmt(b["awb"]), "Shipper", "Tel 852 1234 5678", "Consignee", "Lusaka", b["origin"],
                             b["destination"], "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"],
                             b["declared_value_usd"])
    assert verify.check_image(conn, _png(img)).result == verify.GENUINE


@needs_ocr
def test_a_certificate_without_qr_or_policy_number_reveals_nothing(conn):
    b = _hero_clean(conn)
    p = policies.issue(conn, b["id"])
    img = documents.draw_certificate("Certificate of cargo cover", "", awb.fmt(b["awb"]), "Someone", "NBO to LUN",
                                     "09 Nov 2026", "Pharmaceuticals", 1, 1, "01 Nov 2026")
    res = verify.check_image(conn, _png(img))
    assert res.result != verify.GENUINE
    assert policies.policy_no(p["id"]) not in res.message and "53,648" not in res.message


def test_policy_number_check_never_shows_the_amount(conn):
    b = _hero_clean(conn)
    p = policies.issue(conn, b["id"])
    res = verify.check_policy_number(conn, p["id"], b["awb"])
    assert res.result == verify.GENUINE
    assert "sum_insured_usd" not in res.details and "USD" not in res.message


@needs_qr
def test_bare_domain_qr_is_a_look_alike(conn):
    b = _hero_clean(conn)
    img = documents.draw_certificate("Certificate of cargo cover", "CSP-0000001", awb.fmt(b["awb"]), "Someone",
                                     "NBO to LUN", "09 Nov 2026", "Pharmaceuticals", 1, 1, "01 Nov 2026",
                                     qr_data="cargoshield-check.example/p/AB12")
    assert verify.check_image(conn, _png(img), trusted_hosts={"localhost"}).result == verify.SUSPICIOUS


def test_oversized_uploads_are_refused_before_reading(client):
    r = client.post("/api/chat", content=b"x", headers={"content-length": str(20 * 1024 * 1024),
                                                        "content-type": "application/octet-stream"})
    assert r.status_code == 413


def test_empty_data_label_does_not_break_the_verify_page(client, monkeypatch):
    s = json.loads(json.dumps(config.settings()))
    s["airline"]["data_label"] = None
    monkeypatch.setattr(config, "settings", lambda: s)
    r = client.get("/verify/awb/999-12345675", headers={"accept": "text/html"})
    assert r.status_code == 200


def test_junk_policy_tokens_are_not_counted_as_fakes(client):
    before = client.get("/api/stats").json()["fakes_caught"]
    for t in ("x", "aaaa", "nope"):
        client.get(f"/verify/policy/{t}")
    assert client.get("/api/stats").json()["fakes_caught"] == before


def test_lead_time_is_never_rounded_up_past_the_rule():
    assert _hours_text(11.96) == "11.9"
    assert _hours_text(7.0) == "7"


def test_a_booking_only_hears_about_earlier_history(conn):
    """The hero shipper's gemstones booking was refused before the gold booking
    existed, so it must not be marked with the gold booking's later HIGH score."""
    meta = db.get_meta(conn, "demo")
    hero = conn.execute("SELECT shipper_id, booked_at FROM booking WHERE id = ?", (meta["hero_high_booking_id"],)).fetchone()
    earlier = conn.execute("SELECT r.rule_hits_json FROM booking b JOIN risk_score r ON r.booking_id = b.id "
                           "WHERE b.shipper_id = ? AND b.booked_at < ?", (hero["shipper_id"], hero["booked_at"])).fetchall()
    assert earlier
    for (hits,) in earlier:
        ids = {h["id"] for h in json.loads(hits)}
        assert "shipper_has_other_high_booking" not in ids and "shipper_previously_rejected" not in ids


def test_seeded_departures_sit_on_the_timetable(conn):
    from datetime import timedelta

    from cargoshield.seed import load_routes

    routes = {(r.origin, r.destination): r for r in load_routes()}
    clean = _hero_clean(conn)
    dep = db.parse_dt(clean["booked_at"]) + timedelta(hours=clean["lead_time_hours"])
    r = routes[(clean["origin"], clean["destination"])]
    assert dep == r.next_departure(dep - timedelta(minutes=1))
