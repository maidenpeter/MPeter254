"""Tests for gaps a second review found by breaking the code on purpose
(mutation testing): each one fails if the behaviour it names is removed."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from cargoshield import awb, config, db, documents, graph, ocr, policies, risk, verify
from cargoshield.seed import AIRPORT_TZ, Route, gold_usd_per_kg

needs_ocr = pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")
needs_qr = pytest.mark.skipif(not ocr.qr_available(), reason="zxing-cpp not installed")


def _png(img):
    return documents.png_bytes(img)


# ---------------------------------------------------------------- engine


def test_band_edges():
    assert [risk.band_for(s) for s in (0, 39, 40, 69, 70, 100)] == ["LOW", "LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH"]


def test_model_alone_is_capped_even_when_it_could_score_more(monkeypatch):
    cfg = json.loads(json.dumps(config.rules_config()))
    cfg["anomaly"]["max_points"] = 60
    monkeypatch.setattr(config, "rules_config", lambda: cfg)
    res = risk.combine([], {"percentile": 1.0, "driver": "log_lead_time", "driver_label": "x"})
    assert res.score == 39 and res.band == "LOW"
    assert graph.base_score([{"id": "anomaly_model", "kind": "model", "points": 60}], set(), 39) == 39


def test_vague_dense_cargo_is_not_counted_twice(conn):
    for r in conn.execute("SELECT rule_hits_json FROM risk_score"):
        ids = {h["id"] for h in json.loads(r["rule_hits_json"])}
        assert not ({"vague_description_dense", "weight_volume_mismatch"} <= ids)


def test_reviewer_approval_clears_a_shipper(conn):
    """A HIGH booking a person approved must not flag the shipper, or count against their other bookings."""
    rows = conn.execute(
        """SELECT b.shipper_id FROM review_decision d JOIN booking b ON b.id = d.booking_id
           WHERE d.decision = 'approve'"""
    ).fetchall()
    assert rows, "the seed should include approved HIGH bookings"
    flagged = graph.flagged_shippers(conn)
    for (sid,) in rows:
        other_open_high = conn.execute(
            """SELECT COUNT(*) FROM booking b JOIN risk_score r ON r.booking_id = b.id
               WHERE b.shipper_id = ? AND r.band = 'HIGH'
               AND NOT EXISTS (SELECT 1 FROM review_decision d WHERE d.booking_id = b.id AND d.decision = 'approve')""",
            (sid,)).fetchone()[0]
        kyc = conn.execute("SELECT kyc_status FROM shipper WHERE id = ?", (sid,)).fetchone()[0]
        if other_open_high == 0 and kyc != "failed":
            assert sid not in flagged, sid
        for (hits,) in conn.execute("SELECT r.rule_hits_json FROM booking b JOIN risk_score r ON r.booking_id = b.id "
                                    "WHERE b.shipper_id = ?", (sid,)):
            if other_open_high == 0:
                assert "shipper_has_other_high_booking" not in hits


def test_reject_flags_the_shipper_and_rescores_linked_open_bookings(client, conn):
    # Pick an open, undecided booking of a shipper linked to nobody flagged.
    meta = db.get_meta(conn, "demo")
    ring = meta["ring_shipper_ids"]
    target = conn.execute(
        "SELECT b.id, b.shipper_id FROM booking b JOIN risk_score r ON r.booking_id = b.id "
        "WHERE b.status = 'in_review' AND b.shipper_id NOT IN (%s) LIMIT 1" % ",".join("?" * len(ring)), ring
    ).fetchone()
    if target is None:
        pytest.skip("no undecided HIGH booking outside the ring in this seed")
    r = client.post(f"/api/review/{target['id']}", json={"decision": "reject", "note": "Could not verify"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    c2 = db.connect()
    assert target["shipper_id"] in graph.flagged_shippers(c2)
    c2.close()


def test_approve_moves_a_booking_out_of_the_queue(client):
    q = client.get("/api/review-queue").json()["items"]
    bid = q[-1]["id"]
    r = client.post(f"/api/review/{bid}", json={"decision": "approve", "note": "Checked"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert bid not in [i["id"] for i in client.get("/api/review-queue").json()["items"]]
    assert client.get(f"/api/bookings/{bid}").json()["booking"]["status"] == "accepted"


def test_no_cover_while_on_hold(client, conn):
    clean = db.get_meta(conn, "demo")["hero_clean_booking_id"]
    conn.execute("UPDATE booking SET status = 'on_hold' WHERE id = ?", (clean,))
    conn.commit()
    q = client.get(f"/api/quote/{clean}").json()
    assert not q["eligible"] and "on hold" in q["reason"]


# ---------------------------------------------------------------- graph


def test_two_hop_limit(tmp_path):
    c = sqlite3.connect(str(tmp_path / "chain.db"))
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    for i in range(1, 6):
        c.execute("INSERT INTO shipper (id, name, country, phone, email, created_at, kyc_status) "
                  "VALUES (?, ?, 'KE', ?, ?, '2020-01-01T00:00:00', 'verified')",
                  (i, f"S{i}", f"+254 0000 000 00{i}", f"s{i}@x.example"))
    # chain 1-2-3-4-5 through shared payer refs
    for a, b in ((1, 2), (2, 3), (3, 4), (4, 5)):
        ref = f"ACC-{a}{b}"
        for s in (a, b):
            c.execute("INSERT INTO contact_link VALUES ('shipper', ?, 'payment_ref', ?)", (str(s), ref))
    c.commit()
    ids = lambda sub: {n["shipper_id"] for n in sub["nodes"] if n["kind"] == "shipper"}  # noqa: E731
    assert ids(graph.subgraph(c, 1, hops=2)) == {1, 2, 3}
    assert ids(graph.subgraph(c, 1, hops=1)) == {1, 2}
    assert ids(graph.subgraph(c, 3, hops=2)) == {1, 2, 3, 4, 5}


# ---------------------------------------------------------------- verify endpoints


def test_verify_image_endpoint(client):
    before = client.get("/api/stats").json()["checks_total"]
    fake = client.get("/files/fakes/fake_awb_clearance_fee.png").content
    r = client.post("/verify/image", files={"file": ("fake.png", fake, "image/png")}).json()
    assert r["result"] == ("NOT FOUND" if ocr.tesseract_available() else "UNREADABLE")
    bad = client.post("/verify/image", files={"file": ("x.png", b"not an image", "image/png")}).json()
    assert bad["result"] == "UNREADABLE" and bad["ask"]
    assert client.get("/api/stats").json()["checks_total"] == before + 2


def test_public_policy_check_shows_cover_but_no_shipper_data(client, conn):
    demo = client.get("/api/demo").json()
    pol = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    token = pol["verify_url"].rsplit("/", 1)[1]
    js = client.get(f"/verify/policy/{token}").json()
    assert js["result"] == "GENUINE"
    assert set(js["details"]) == {"policy_no", "status", "awb", "route", "flight_date", "sum_insured_usd",
                                  "issued_at", "cover_ends"}
    html = client.get(f"/verify/policy/{token}", headers={"accept": "text/html"}).text
    s = conn.execute("SELECT s.name, s.phone, s.email FROM shipper s JOIN booking b ON b.shipper_id = s.id "
                     "WHERE b.id = ?", (demo["hero_clean_booking_id"],)).fetchone()
    for secret in s:
        assert secret not in json.dumps(js) and secret not in html
    assert "match your certificate" in html  # the 'check it matches' warning survives on the scan page
    conn.execute("UPDATE policy SET status = 'cancelled' WHERE id = ?", (pol["id"],))
    conn.commit()
    assert client.get(f"/verify/policy/{token}").json()["result"] == "NOT ACTIVE"


def test_old_cover_reads_as_ended(conn):
    p = conn.execute("SELECT p.qr_token FROM policy p JOIN booking b ON b.id = p.booking_id "
                     "WHERE b.flight_date < date('now', '-40 day') LIMIT 1").fetchone()
    if p is None:
        pytest.skip("no seeded cover old enough in this seed")
    res = verify.check_policy_token(conn, p["qr_token"])
    assert res.result == verify.NOT_ACTIVE and "ended" in res.message


def test_http_checks_are_logged(client, conn):
    demo = client.get("/api/demo").json()
    pol = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    token = pol["verify_url"].rsplit("/", 1)[1]
    before = client.get("/api/stats").json()["checks_total"]
    client.get("/verify/awb/999-12345675")
    client.get(f"/verify/policy/{token}")
    client.get(f"/verify/policy/{token}", headers={"accept": "text/html"})
    client.post("/api/chat", data={"text": f"https://evil.example/verify/policy/{token}"})
    assert client.get("/api/stats").json()["checks_total"] == before + 4


@needs_qr
def test_issued_qr_holds_the_verify_link_and_tokens_are_fresh(client):
    demo = client.get("/api/demo").json()
    pol = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    png = client.get(pol["qr_png"]).content
    assert ocr.read_qr(ocr.load_image(png)) == [pol["verify_url"]]
    client.post("/api/admin/reset")
    again = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    assert again["id"] == pol["id"] and again["verify_url"] != pol["verify_url"]


def test_kpis_follow_what_happens(client):
    s = client.get("/api/stats").json()
    assert s["review_pending"] == len(client.get("/api/review-queue").json()["items"])
    demo = client.get("/api/demo").json()
    pol = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    s2 = client.get("/api/stats").json()
    assert round(s2["premium_written_usd"] - s["premium_written_usd"], 2) == pol["premium_usd"]
    assert s2["policies_issued"] == s["policies_issued"] + 1


def test_typed_numbers_are_not_counted_as_fakes(client):
    before = client.get("/api/stats").json()["fakes_caught"]
    client.post("/api/chat", data={"text": "999-12345675"})  # the chat's own example: NOT FOUND, but typed
    client.post("/api/chat", data={"text": "176-12345675"})  # another airline's number
    assert client.get("/api/stats").json()["fakes_caught"] == before


def test_payment_reference_is_masked(client):
    demo = client.get("/api/demo").json()
    ref = client.get(f"/api/bookings/{demo['hero_clean_booking_id']}").json()["booking"]["payment_ref"]
    assert ref and "•" in ref


# ---------------------------------------------------------------- altered paperwork


@needs_ocr
def test_real_awb_with_altered_route_is_suspicious(conn):
    b = conn.execute("SELECT * FROM booking WHERE id = ?", (db.get_meta(conn, "demo")["hero_clean_booking_id"],)).fetchone()
    img = documents.draw_awb(awb.fmt(b["awb"]), "Shipper", "Nairobi", "Consignee", "Lagos", "NBO NAIROBI", "LOS LAGOS",
                             "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"], b["declared_value_usd"])
    assert verify.check_image(conn, _png(img)).result == verify.SUSPICIOUS


@needs_ocr
def test_real_awb_with_a_fee_demand_is_suspicious(conn):
    b = conn.execute("SELECT * FROM booking WHERE id = ?", (db.get_meta(conn, "demo")["hero_clean_booking_id"],)).fetchone()
    img = documents.draw_awb(awb.fmt(b["awb"]), "Shipper", "Nairobi", "Consignee", "Lusaka", b["origin"], b["destination"],
                             "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"], b["declared_value_usd"],
                             scam_note="Release on payment of USD 900 clearing fee to agent")
    res = verify.check_image(conn, _png(img))
    assert res.result == verify.SUSPICIOUS and "fee" in res.message


@needs_ocr
@needs_qr
def test_qr_for_another_awb_on_real_paperwork_is_suspicious(conn):
    meta = db.get_meta(conn, "demo")
    b = conn.execute("SELECT * FROM booking WHERE id = ?", (meta["hero_clean_booking_id"],)).fetchone()
    other = conn.execute("SELECT awb FROM booking WHERE id != ? AND status = 'accepted' LIMIT 1", (b["id"],)).fetchone()["awb"]
    page = documents.draw_awb(awb.fmt(b["awb"]), "Shipper", "Nairobi", "Consignee", "Lusaka", b["origin"],
                              b["destination"], "09 NOV 2026", b["pieces"], b["weight_kg"], b["commodity_text"],
                              b["declared_value_usd"])
    qr = documents.qr_image(f"http://localhost:8000/verify/awb/{awb.fmt(other)}").resize((260, 260))
    page.paste(qr, (1300, 160))
    assert verify.check_image(conn, _png(page), trusted_hosts={"localhost"}).result == verify.SUSPICIOUS


@needs_ocr
@needs_qr
def test_genuine_qr_on_a_certificate_with_a_random_printed_number_is_suspicious(conn):
    p = policies.issue(conn, db.get_meta(conn, "demo")["hero_clean_booking_id"])
    forged = documents.draw_certificate("Certificate of cargo cover", policies.policy_no(p["id"]), "999-81234560",
                                        "Victim Ltd", "NBO to LUN", "2026-11-09", "Pharmaceuticals", 2_000_000, 300,
                                        "2026-11-04", qr_data=policies.verify_url("http://localhost:8000", p["qr_token"]))
    assert verify.check_image(conn, _png(forged), trusted_hosts={"localhost"}).result == verify.SUSPICIOUS


# ---------------------------------------------------------------- data realism


def test_route_times_are_local_to_the_origin():
    r = Route("NBO", "JNB", frozenset(range(7)), datetime(2026, 1, 1, 23, 40).time(), AIRPORT_TZ["NBO"])
    dep = r.next_departure(datetime(2026, 11, 5, 12, 0))  # UTC
    assert dep == datetime(2026, 11, 5, 20, 40)  # 23:40 in Nairobi is 20:40 UTC
    assert r.local_date(dep).isoformat() == "2026-11-05"
    hk = Route("HKG", "NBO", frozenset(range(7)), datetime(2026, 1, 1, 2, 10).time(), AIRPORT_TZ["HKG"])
    dep = hk.next_departure(datetime(2026, 11, 5, 12, 0))
    assert dep == datetime(2026, 11, 5, 18, 10) and hk.local_date(dep).isoformat() == "2026-11-06"


def test_gold_is_priced_from_the_configured_spot(conn):
    per_kg = gold_usd_per_kg()
    for r in conn.execute("SELECT weight_kg, declared_value_usd FROM booking WHERE commodity_text = 'Gold bars'"):
        vpk = r["declared_value_usd"] / r["weight_kg"]
        assert 0.95 * per_kg <= vpk <= 1.02 * per_kg


def test_reviewer_notes_match_the_booking(conn):
    rows = conn.execute("SELECT d.note, r.rule_hits_json, d.decision FROM review_decision d "
                        "JOIN risk_score r ON r.booking_id = d.booking_id").fetchall()
    assert rows
    for r in rows:
        ids = {h["id"] for h in json.loads(r["rule_hits_json"])}
        if "watchlist" in r["note"]:
            assert "watchlist_shipper" in ids
        if "Contents did not match" in r["note"]:
            assert ids & {"vague_description_dense", "weight_volume_mismatch", "implausible_weight"}


def test_referred_quotes_cannot_be_bought(client, conn):
    row = conn.execute(
        "SELECT b.id FROM booking b JOIN risk_score r ON r.booking_id = b.id JOIN shipper s ON s.id = b.shipper_id "
        "WHERE r.band = 'MEDIUM' AND b.status = 'accepted' AND s.kyc_status != 'verified' "
        "AND datetime(b.booked_at, '+' || CAST(b.lead_time_hours AS TEXT) || ' hours') > datetime('now') LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("no future MEDIUM booking from an unverified shipper in this seed")
    q = client.get(f"/api/quote/{row['id']}").json()
    assert q["eligible"] and not q["bindable"] and q["referral_reasons"]
    r = client.post("/api/policies", json={"booking_id": row["id"]})
    assert r.status_code == 409 and "underwriter" in r.json()["detail"]


def test_one_active_policy_per_booking(conn):
    p = policies.issue(conn, db.get_meta(conn, "demo")["hero_clean_booking_id"])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO policy (booking_id, sum_insured_usd, premium_usd, status, qr_token, issued_at) "
                     "VALUES (?, 1, 1, 'active', 'x', '2026-01-01T00:00:00')", (p["booking_id"],))
    conn.rollback()


def test_fake_documents_are_dated_around_the_seed(conn):
    if not ocr.tesseract_available():
        pytest.skip("Tesseract not installed")
    anchor = db.parse_dt(db.get_meta(conn, "anchor"))
    text = ocr.read_text(ocr.load_image((config.fakes_dir() / "fake_awb_clearance_fee.png").read_bytes()))
    expected = (anchor - timedelta(days=2)).strftime("%d %b %Y").upper()
    assert expected in text.upper().replace("0CT", "OCT")
