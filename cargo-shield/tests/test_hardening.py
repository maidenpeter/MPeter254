"""Regression tests for problems found in the independent review."""

import io
from datetime import datetime, timedelta

import pytest
from PIL import Image, ImageDraw

from cargoshield import awb, commodity, config, db, documents, messaging, ocr, policies, pricing, seed, verify

needs_qr = pytest.mark.skipif(not ocr.qr_available(), reason="zxing-cpp not installed")
needs_ocr = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")
M = messaging.IncomingMessage


def _png(img):
    return documents.png_bytes(img)


def _issue_clean(conn):
    return policies.issue(conn, db.get_meta(conn, "demo")["hero_clean_booking_id"])


# ---------------------------------------------------------------- demo timing


def test_hero_flight_is_always_still_to_fly():
    """Whatever time the demo is seeded, the HIGH booking's plane hasn't left."""
    start = datetime(2026, 11, 2)  # a Monday
    for q in range(7 * 24 * 4):  # every 15 minutes for a week
        anchor = start + timedelta(minutes=15 * q)
        g = seed.Generator(254, anchor, 45)
        route, dep, booked = seed.pick_hero_flight(g)
        assert dep > anchor, anchor
        assert booked <= anchor, anchor
        assert (dep - booked) < timedelta(hours=12), anchor
        assert route.flies_on(dep), anchor
        assert route.origin == "NBO", anchor


def test_seed_survives_a_different_route_file(tmp_path, monkeypatch):
    """Refreshing routes.csv (as the README asks) must not break the seed."""
    cfg = tmp_path / "config"
    import shutil

    shutil.copytree(config.config_dir(), cfg)
    routes = tmp_path / "routes.csv"
    routes.write_text("origin,destination,days_of_week,departure_local\n"
                      "NBO,DXB,Mon|Thu,02:15\nDXB,NBO,Tue|Fri,09:40\nNBO,EBB,Wed,13:00\n", encoding="utf-8")
    text = (cfg / "settings.yaml").read_text(encoding="utf-8").replace("routes_csv: data/routes.csv", f"routes_csv: {routes}")
    (cfg / "settings.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("CARGOSHIELD_CONFIG_DIR", str(cfg))
    config.reload()
    try:
        for anchor in (datetime(2026, 11, 5, 10), datetime(2026, 11, 7, 15)):
            g = seed.Generator(254, anchor, 45)
            route, dep, booked = seed.pick_hero_flight(g)
            assert dep > anchor and booked <= anchor
    finally:
        monkeypatch.delenv("CARGOSHIELD_CONFIG_DIR")
        config.reload()


# ---------------------------------------------------------------- commodity


@pytest.mark.parametrize("text,expected", [
    ("Cashew nuts", "general_cargo"), ("Cashmere sweaters", "garments"), ("Marigold flowers", "perishables"),
    ("Golden syrup", "general_cargo"), ("Headphones", "general_cargo"), ("Pumpkins", "general_cargo"),
    ("Gold bars", "valuables"), ("Bank notes", "valuables"), ("Smartphones", "mobile_phones"),
    ("Pharmaceuticals", "pharma"), ("Spare parts", "machinery_parts"),
])
def test_classifier_matches_whole_words(text, expected):
    assert commodity.classify(text) == expected


def test_every_generator_description_maps_back_to_its_class():
    for cls, spec in commodity.classes().items():
        for d in spec["descriptions"]:
            assert commodity.classify(d) == cls, d


# ---------------------------------------------------------------- cover


def test_no_cover_after_departure(conn):
    past = conn.execute(
        "SELECT b.id FROM booking b JOIN risk_score r ON r.booking_id = b.id "
        "WHERE r.band = 'LOW' AND b.status = 'accepted' AND b.flight_date < date('now', '-2 day') "
        "AND b.declared_value_usd > b.weight_kg * 40 + 2000 "
        "AND NOT EXISTS (SELECT 1 FROM policy p WHERE p.booking_id = b.id) LIMIT 1"
    ).fetchone()["id"]
    q = policies.quote_booking(conn, past)
    assert not q.eligible and "before departure" in q.reason
    with pytest.raises(policies.CoverError):
        policies.issue(conn, past)


def test_seeded_policies_were_bought_before_departure(conn):
    for r in conn.execute("SELECT p.issued_at, b.booked_at, b.lead_time_hours FROM policy p JOIN booking b ON b.id = p.booking_id"):
        departure = datetime.fromisoformat(r["booked_at"]) + timedelta(hours=r["lead_time_hours"])
        assert datetime.fromisoformat(r["issued_at"]) < departure


def test_premium_never_exceeds_sum_insured_and_lines_add_up_exactly(conn):
    from decimal import Decimal

    n = 0
    for r in conn.execute("SELECT b.*, r.band FROM booking b JOIN risk_score r ON r.booking_id = b.id "
                          "WHERE r.band IN ('LOW', 'MEDIUM')"):
        q = pricing.quote_for_row(r, r["band"])
        if not q.eligible:
            continue
        n += 1
        D = lambda x: Decimal(str(x))  # noqa: E731
        assert q.premium_usd < q.sum_insured_usd
        assert D(q.expected_loss_usd) + D(q.expense_usd) + D(q.profit_usd) == D(q.gross_premium_usd)
        assert D(q.declared_value_usd) - D(q.carrier_liability_usd) == D(q.sum_insured_usd)
        loading = 1 - D(q.expense_loading) - D(q.profit_margin)
        assert (D(q.expected_loss_usd) / loading).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP") == D(q.gross_premium_usd)
    assert n > 500


def test_tiny_top_up_is_not_offered():
    q = pricing.quote(290, 8.2, "documents", "LOW")
    assert not q.eligible and "minimum" in q.reason


# ---------------------------------------------------------------- verification leaks


def test_public_status_does_not_reveal_the_review_flag(conn):
    high = db.get_meta(conn, "demo")["hero_high_awb"]
    low = conn.execute("SELECT awb FROM booking b JOIN risk_score r ON r.booking_id = b.id "
                       "WHERE r.band = 'LOW' AND b.status = 'accepted' AND b.flight_date > date('now', '+1 day') "
                       "LIMIT 1").fetchone()["awb"]
    a, b = verify.check_awb(conn, high), verify.check_awb(conn, low)
    assert a.result == b.result == verify.GENUINE
    assert a.details["carriage_status"] == b.details["carriage_status"]


def test_rejected_booking_is_not_active_not_genuine(conn):
    row = conn.execute("SELECT awb FROM booking WHERE status = 'rejected' LIMIT 1").fetchone()
    res = verify.check_awb(conn, row["awb"])
    assert res.result == verify.NOT_ACTIVE and "do not pay" in res.message.lower()
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text=awb.fmt(row["awb"])))
    assert "✅" not in out.text


def test_policy_number_alone_reveals_nothing(conn):
    p = _issue_clean(conn)
    no = policies.policy_no(p["id"])
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text=no))
    assert out.result == verify.UNREADABLE and "air waybill" in out.text and "USD" not in out.text
    real_awb = conn.execute("SELECT awb FROM booking WHERE id = ?", (p["booking_id"],)).fetchone()["awb"]
    ok = messaging.handle_message(conn, M("whatsapp-sim", "u", text=f"{no} {awb.fmt(real_awb)}"))
    assert ok.result == verify.GENUINE
    wrong = messaging.handle_message(conn, M("whatsapp-sim", "u", text=f"{no} 999-12345675"))
    assert wrong.result == verify.SUSPICIOUS and "USD" not in wrong.text


def test_unicode_digits_do_not_crash(client):
    assert client.get("/verify/awb/9991234567%C2%B2").status_code == 200


def test_phone_numbers_are_not_stored_in_the_log(conn):
    messaging.handle_message(conn, M("whatsapp-sim", "u", text="call me on +254 712 345 678"))
    last = conn.execute("SELECT input_value FROM verification_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "712" not in last


# ---------------------------------------------------------------- links and QR codes


@pytest.mark.parametrize("template", [
    "https://cargoshield-verify.example/verify/policy/{t}",
    "please check https://evil.example/verify/policy/{t}",
    "https://evil.example@localhost/verify/policy/{t}",
    "https://evil\\@localhost/verify/policy/{t}",
    "cargoshield-verify.example/verify/policy/{t}",
    "https://localhost:8000@evil.example/verify/policy/{t}",
    "http://127.0.0.1:80@cargoshield-verify.example/verify/policy/{t}",
])
def test_foreign_or_malformed_links_in_chat_are_suspicious(conn, template):
    token = _issue_clean(conn)["qr_token"]
    out = messaging.handle_message(conn, M("whatsapp-sim", "u", text=template.format(t=token)))
    assert out.result == verify.SUSPICIOUS


@needs_qr
@pytest.mark.parametrize("payload", [
    "Scan to verify: https://evil.example/verify/policy/{t}",
    "cargoshield-verify.example/verify/policy/{t}",
    "https://evil\\@localhost/verify/policy/{t}",
    "https://demo-airline-cargo.example/track?awb={awb}",
    "https://localhost:8000@evil.example/verify/policy/{t}",
])
def test_look_alike_qr_codes_are_suspicious(conn, payload):
    token = _issue_clean(conn)["qr_token"]
    hero_awb = awb.fmt(db.get_meta(conn, "demo")["hero_clean_awb"])
    img = documents.draw_certificate("Certificate of cargo cover", "CSP-0000001", hero_awb, "Someone", "NBO to LUN",
                                     "2026-11-09", "Pharmaceuticals", 50000, 300, "2026-11-04",
                                     qr_data=payload.format(t=token, awb=hero_awb))
    res = verify.check_image(conn, _png(img), trusted_hosts={"localhost"})
    assert res.result == verify.SUSPICIOUS, res.message


@needs_qr
@needs_ocr
def test_genuine_qr_copied_onto_a_forged_certificate_is_suspicious(conn):
    p = _issue_clean(conn)
    other = conn.execute("SELECT awb FROM booking WHERE id != ? AND status = 'accepted' LIMIT 1",
                         (p["booking_id"],)).fetchone()["awb"]
    forged = documents.draw_certificate("Certificate of cargo cover", policies.policy_no(p["id"]), awb.fmt(other),
                                        "Victim Pharma Ltd", "NBO to LUN", "2026-11-09", "Pharmaceuticals",
                                        2_500_000, 300, "2026-11-04",
                                        qr_data=policies.verify_url("http://localhost:8000", p["qr_token"]))
    res = verify.check_image(conn, _png(forged), trusted_hosts={"localhost"})
    assert res.result == verify.SUSPICIOUS, res.message


@needs_ocr
def test_awb_mentioning_insurance_is_not_mistaken_for_a_certificate(conn):
    meta = db.get_meta(conn, "demo")
    b = conn.execute("SELECT * FROM booking WHERE id = ?", (meta["hero_clean_booking_id"],)).fetchone()
    img = documents.draw_awb(awb.fmt(b["awb"]), "Shipper", "Nairobi", "Consignee", "Lusaka", b["origin"],
                             b["destination"], "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"],
                             b["declared_value_usd"], note="Amount of insurance: NIL")
    assert verify.check_image(conn, _png(img)).result == verify.GENUINE


def test_huge_images_are_refused_quickly(conn):
    big = Image.new("L", (6000, 6000), 255)  # 36 Mpx, over the 25 Mpx limit
    buf = io.BytesIO()
    big.save(buf, format="PNG", optimize=True)
    res = verify.check_image(conn, buf.getvalue())
    assert res.result == verify.UNREADABLE and "smaller" in res.message


# ---------------------------------------------------------------- app guard


def test_staff_pages_are_closed_to_other_devices(seeded):
    from fastapi.testclient import TestClient

    from cargoshield.app import app

    with TestClient(app, base_url="http://localhost", client=("192.168.1.20", 50000)) as phone:
        assert phone.get("/api/stats").status_code == 403
        assert phone.post("/api/admin/reset").status_code == 403
        assert phone.get("/api/policies/1/qr.png").status_code == 403
        assert phone.get("/graph/shipper/1").status_code == 403
        for path in ("/", "/api/bookings", "/api/review-queue", "/api/verifications", "/api/documents",
                     "/api/demo", "/files/fakes/fake_awb_clearance_fee.png", "/static/app.js", "/docs"):
            assert phone.get(path).status_code == 403, path
        assert phone.post("/api/policies", json={"booking_id": 1}).status_code == 403
        assert phone.get("/verify/awb/999-12345675").status_code == 200  # public


def test_cross_site_posts_are_refused(client):
    r = client.post("/api/admin/reset", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/admin/reset", headers={"origin": "http://localhost"}).status_code == 200


def test_booking_api_refuses_fake_and_foreign_awbs(client, conn):
    sid = conn.execute("SELECT id FROM shipper LIMIT 1").fetchone()[0]
    body = {"shipper_id": sid, "origin": "NBO", "destination": "JNB",
            "commodity_text": "Documents", "pieces": 1, "weight_kg": 2, "volume_m3": 0.01,
            "declared_value_usd": 50, "payment_method": "card", "lead_time_hours": 48}
    from cargoshield import fakes

    assert client.post("/api/bookings", json={**body, "awb": fakes.fake_awb(fakes.FAKES[0])}).status_code == 422
    assert client.post("/api/bookings", json={**body, "awb": "176-12345675"}).status_code == 422


def test_engine_report_ignores_live_bookings(client):
    before = client.get("/api/demo").json()["engine_check"]
    for _ in range(5):
        client.post("/api/demo/simulate")
    after = client.get("/api/demo").json()["engine_check"]
    assert after["clean_total"] == before["clean_total"]
    assert after["fp_rate_medium_plus"] == before["fp_rate_medium_plus"]


def test_graph_node_ids_do_not_encode_contact_details(client, conn):
    import hashlib

    ring = db.get_meta(conn, "demo")["ring_shipper_ids"]
    g = client.get(f"/graph/shipper/{ring[0]}").json()
    phones = [r[0] for r in conn.execute("SELECT phone FROM shipper")]
    hashes = {hashlib.sha256(p.encode()).hexdigest()[:12] for p in phones}
    for n in g["nodes"]:
        assert n["id"].split(":", 1)[1] not in hashes
