"""Milestone 6: the dashboard API."""

from pathlib import Path

from cargoshield import config, db


def test_index_and_static_assets(client):
    r = client.get("/")
    assert r.status_code == 200 and "CargoShield" in r.text
    assert "Demo data built on the public flight schedule" in r.text
    for asset in ("/static/app.js", "/static/styles.css", "/static/vendor/vis-network.min.js"):
        assert client.get(asset).status_code == 200, asset


def test_page_loads_nothing_from_the_internet(client):
    """The demo must run offline: no CDN scripts, fonts or styles."""
    import re

    html = client.get("/").text
    assert not re.search(r"""(src|href)=["']https?://""", html)
    js = client.get("/static/app.js").text
    assert "https://" not in js and "http://" not in js


def test_stats(client):
    s = client.get("/api/stats").json()
    assert s["screened"] == 3000
    assert s["flagged_high"] > 0 and s["policies_issued"] > 0 and s["fakes_caught"] > 0


def test_booking_list_filters(client):
    all_ = client.get("/api/bookings?limit=5").json()
    assert all_["total"] == 3000 and len(all_["items"]) == 5
    high = client.get("/api/bookings?band=HIGH&limit=500").json()
    assert high["items"] and all(b["band"] == "HIGH" for b in high["items"])
    flagged = client.get("/api/bookings?band=FLAGGED&limit=1").json()
    assert flagged["total"] > high["total"]
    hit = client.get(f"/api/bookings?q={config.awb_prefix()}-60422180").json()
    assert hit["total"] == 1


def test_booking_detail_has_reasons_quote_and_masked_shipper(client):
    demo = client.get("/api/demo").json()
    d = client.get(f"/api/bookings/{demo['hero_high_booking_id']}").json()
    assert d["risk"]["band"] == "HIGH" and len(d["risk"]["reasons"]) >= 3
    assert d["quote"]["eligible"] is False
    assert "•" in d["shipper"]["phone"] and "•" in d["shipper"]["email"]
    c = client.get(f"/api/bookings/{demo['hero_clean_booking_id']}").json()
    assert c["risk"]["band"] == "LOW" and c["quote"]["eligible"] is True
    assert client.get("/api/bookings/999999").status_code == 404


def test_review_queue_and_decisions(client):
    q = client.get("/api/review-queue").json()
    assert q["items"] and all(i["band"] == "HIGH" for i in q["items"])
    bid = q["items"][0]["id"]
    assert client.post(f"/api/review/{bid}", json={"decision": "reject"}).status_code == 422  # note required
    r = client.post(f"/api/review/{bid}", json={"decision": "hold", "note": "Call shipper"})
    assert r.json()["status"] == "on_hold"
    r = client.post(f"/api/review/{bid}", json={"decision": "reject", "note": "Could not verify the shipper"})
    assert r.json()["status"] == "rejected"
    assert client.post(f"/api/review/{bid}", json={"decision": "approve"}).status_code == 409
    q2 = client.get("/api/review-queue").json()
    assert bid not in [i["id"] for i in q2["items"]]
    assert q2["recent_decisions"][0]["decision"] == "reject"


def test_screen_a_new_booking_via_api(client, conn):
    sid = conn.execute("SELECT id FROM shipper WHERE kyc_status = 'verified' LIMIT 1").fetchone()[0]
    body = {"shipper_id": sid, "origin": "NBO", "destination": "JNB",
            "commodity_text": "Documents", "pieces": 20, "weight_kg": 400, "volume_m3": 1.3,
            "declared_value_usd": 900, "payment_method": "bank_transfer", "lead_time_hours": 48}
    r = client.post("/api/bookings", json=body)
    assert r.status_code == 201
    d = r.json()
    assert d["risk"]["band"] in ("MEDIUM", "HIGH")
    assert any(h["id"] == "implausible_weight" for h in d["risk"]["hits"])


def test_live_feed_adds_a_scored_booking(client):
    before = client.get("/api/stats").json()["screened"]
    b = client.post("/api/demo/simulate").json()
    assert b["band"] in ("LOW", "MEDIUM", "HIGH")
    assert client.get("/api/stats").json()["screened"] == before + 1


def test_issue_cover_and_files(client):
    demo = client.get("/api/demo").json()
    p = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]})
    assert p.status_code == 201
    pol = p.json()
    assert client.get(pol["qr_png"]).headers["content-type"] == "image/png"
    assert client.get(pol["certificate_png"]).headers["content-type"] == "image/png"
    assert client.post("/api/policies", json={"booking_id": demo["hero_high_booking_id"]}).status_code == 409


def test_document_files_are_whitelisted(client):
    docs = client.get("/api/documents").json()
    assert len(docs["fakes"]) == 8 and docs["samples"]
    assert client.get(docs["fakes"][0]["url"]).status_code == 200
    assert client.get("/files/fakes/../cargoshield.db").status_code == 404
    assert client.get("/files/samples/anything.png").status_code == 404


def test_reset_restores_the_starting_state(client):
    start = client.get("/api/stats").json()
    client.post("/api/demo/simulate")
    demo = client.get("/api/demo").json()
    client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]})
    assert client.get("/api/stats").json() != start
    assert client.post("/api/admin/reset").json()["ok"]
    assert client.get("/api/stats").json() == start


def test_schema_files_define_the_same_tables_and_columns():
    import re

    root = Path(__file__).resolve().parent.parent / "cargoshield"

    def shape(sql: str) -> dict:
        out = {}
        for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
            cols = [line.split()[0] for line in body.strip().splitlines()
                    if line.strip() and not line.strip().startswith(("PRIMARY", "--", "CHECK"))]
            out[name] = cols
        return out

    lite = shape((root / "schema.sql").read_text(encoding="utf-8"))
    pg = shape((root / "schema_postgres.sql").read_text(encoding="utf-8"))
    assert lite == pg and len(lite) == 9
