"""Regression tests for the third review round."""

import io
import os
from datetime import datetime, timedelta

import pytest
from PIL import Image

from cargoshield import awb, config, db, documents, ocr, policies, seed, verify

needs_ocr = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")
needs_qr = pytest.mark.skipif(not ocr.qr_available(), reason="zxing-cpp not installed")


def _png(img):
    return documents.png_bytes(img)


def _hero_clean(conn):
    return conn.execute("SELECT * FROM booking WHERE id = ?", (db.get_meta(conn, "demo")["hero_clean_booking_id"],)).fetchone()


@pytest.fixture()
def scratch_data(tmp_path, monkeypatch):
    """A separate data folder, so a test can seed without touching the session's demo."""
    monkeypatch.setenv("CARGOSHIELD_DATA_DIR", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------- seeding


@pytest.mark.parametrize("hour", [3, 9, 14, 20])
def test_decisions_and_scores_agree_whenever_you_seed(scratch_data, hour):
    seed.run(n_bookings=900, quiet=True, anchor=datetime(2026, 11, 5, hour, 15))
    c = db.connect()
    bad = c.execute("SELECT COUNT(*) FROM review_decision d JOIN risk_score r ON r.booking_id = d.booking_id "
                    "WHERE r.band != 'HIGH'").fetchone()[0]
    assert bad == 0
    hero = db.get_meta(c, "demo")["hero_high_booking_id"]
    h = c.execute("SELECT booked_at, lead_time_hours, status FROM booking WHERE id = ?", (hero,)).fetchone()
    departure = db.parse_dt(h["booked_at"]) + timedelta(hours=h["lead_time_hours"])
    assert departure >= datetime(2026, 11, 5, hour, 15) + timedelta(hours=4)  # time to present
    assert h["status"] == "in_review"
    c.close()


def test_a_failed_reseed_keeps_the_old_demo(scratch_data, monkeypatch):
    seed.run(n_bookings=600, quiet=True)
    before = os.path.getmtime(config.snapshot_path()), os.path.getmtime(config.model_path())
    c = db.connect()
    n = c.execute("SELECT COUNT(*) FROM booking").fetchone()[0]
    c.close()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(seed, "seed_history", boom)
    with pytest.raises(RuntimeError):
        seed.run(n_bookings=900, quiet=True)
    assert (os.path.getmtime(config.snapshot_path()), os.path.getmtime(config.model_path())) == before
    c = db.connect()
    assert c.execute("SELECT COUNT(*) FROM booking").fetchone()[0] == n
    c.close()


def test_shippers_scale_with_bookings():
    import inspect

    src = inspect.getsource(seed.run)
    assert "n_bookings / 7.5" in src


# ---------------------------------------------------------------- cover after review


def test_refusing_a_booking_cancels_its_cover(client, conn):
    row = conn.execute("SELECT b.id FROM booking b JOIN risk_score r ON r.booking_id = b.id "
                       "WHERE b.status = 'in_review' LIMIT 1").fetchone()
    # Give it cover first (as if it had been bought while the booking was still LOW).
    conn.execute("INSERT INTO policy (booking_id, sum_insured_usd, premium_usd, status, qr_token, issued_at) "
                 "VALUES (?, 1000, 30, 'active', 'tok-refused-booking-000000000000', '2026-11-01T00:00:00')", (row["id"],))
    conn.commit()
    client.post(f"/api/review/{row['id']}", json={"decision": "reject", "note": "Refused"})
    res = client.get("/verify/policy/tok-refused-booking-000000000000").json()
    assert res["result"] == "NOT ACTIVE"


def test_held_booking_never_reads_flown(conn):
    row = conn.execute("SELECT b.* FROM booking b WHERE b.status = 'in_review' LIMIT 1").fetchone()
    conn.execute("UPDATE booking SET booked_at = ?, lead_time_hours = 2 WHERE id = ?",
                 (db.iso(db.utcnow() - timedelta(days=2)), row["id"]))
    conn.commit()
    assert verify.check_awb(conn, row["awb"]).details["carriage_status"] == "Booked"


# ---------------------------------------------------------------- paperwork checks


@needs_ocr
def test_ordinary_charge_wording_is_not_a_scam(conn):
    b = _hero_clean(conn)
    img = documents.draw_awb(awb.fmt(b["awb"]), "Shipper", "Nairobi", "Consignee", "Lusaka", b["origin"],
                             b["destination"], "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"],
                             b["declared_value_usd"], scam_note="Handling charge USD 45. Storage charges apply after 48 h")
    assert verify.check_image(conn, _png(img)).result == verify.GENUINE


@needs_ocr
@needs_qr
def test_genuine_certificate_with_a_fee_demand_is_suspicious(conn):
    p = policies.issue(conn, _hero_clean(conn)["id"])
    png = policies.certificate_png(conn, p["id"], "http://localhost:8000")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    from PIL import ImageDraw

    ImageDraw.Draw(img).text((520, 1330), "Release on payment of USD 900 to our agent", font=documents.font(30), fill=(176, 32, 38))
    assert verify.check_image(conn, _png(img), trusted_hosts={"localhost"}).result == verify.SUSPICIOUS


@needs_ocr
def test_two_air_waybill_numbers_on_one_page_is_suspicious(conn):
    b = _hero_clean(conn)
    other = awb.fmt(awb.make("999", "2718281"))
    img = documents.draw_awb(other, "Shipper", "Nairobi", "Consignee", "Lusaka", b["origin"], b["destination"],
                             "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"], b["declared_value_usd"],
                             scam_note=f"Ref {awb.fmt(b['awb'])}")
    assert verify.check_image(conn, _png(img)).result == verify.SUSPICIOUS


@needs_ocr
def test_other_airline_number_on_paper_that_names_us_is_suspicious(conn):
    img = documents.draw_awb("176-12345675", "Shipper", "Nairobi", "Consignee", "Lagos", "NBO NAIROBI", "LOS LAGOS",
                             "09 NOV 2026", 3, 40, "Garments", 900, carrier=config.airline_name())
    assert verify.check_image(conn, _png(img)).result == verify.SUSPICIOUS
    cert = documents.draw_certificate("Certificate of cargo insurance", "XY-1", "176-12345675", "Someone",
                                      "NBO to LOS", "09 Nov 2026", "Garments", 900, 20, "01 Nov 2026",
                                      issuer="Another insurer", note="Sample")
    assert verify.check_image(conn, _png(cert)).result == verify.OTHER_AIRLINE


def test_long_thin_images_are_refused(conn):
    buf = io.BytesIO()
    Image.new("L", (4000, 20), 255).save(buf, format="PNG")
    res = verify.check_image(conn, buf.getvalue())
    assert res.result == verify.UNREADABLE and "shape" in res.message


# ---------------------------------------------------------------- app


def test_unknown_host_header_is_refused(client):
    assert client.get("/api/stats", headers={"host": "attacker.example"}).status_code == 400
    assert client.get("/verify/awb/999-12345675", headers={"host": "attacker.example"}).status_code == 400
    assert client.get("/api/stats", headers={"host": "localhost:8000"}).status_code == 200


def test_review_counter_keeps_holds_apart(client):
    q = client.get("/api/review-queue").json()["items"]
    before = client.get("/api/stats").json()
    client.post(f"/api/review/{q[0]['id']}", json={"decision": "hold", "note": "Waiting for documents"})
    after = client.get("/api/stats").json()
    assert after["review_decided"] == before["review_decided"]
    assert after["review_on_hold"] == before["review_on_hold"] + 1


def test_review_queue_is_soonest_departure_first(client):
    items = [i for i in client.get("/api/review-queue").json()["items"] if i["status"] == "in_review"]
    deps = [i["departure_at"] for i in items]
    assert deps == sorted(deps)


def test_verify_page_labels_every_field(client):
    demo = client.get("/api/demo").json()
    pol = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    html = client.get(pol["verify_url"].replace("http://testserver", ""), headers={"accept": "text/html"}).text
    assert "cover_ends" not in html and "Cover ends" in html


def test_doctor_checks_time_zone_data_for_real():
    import inspect

    from cargoshield import __main__ as cli

    assert 'ZoneInfo("Africa/Nairobi")' in inspect.getsource(cli.cmd_doctor)


def test_routes_placeholder_is_detected():
    assert seed.routes_are_placeholder()
