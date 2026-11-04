"""Milestone 7: the 3-minute script in DEMO.md, end to end through the API."""

import pytest

from cargoshield import ocr


@pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")
def test_three_minute_script(client):
    demo = client.get("/api/demo").json()
    start = client.get("/api/stats").json()

    # Step 1: a fake AWB photo goes into the chat simulator -> NOT FOUND, don't pay.
    fake = demo["demo_fake"]
    img = client.get(f"/files/fakes/{fake}").content
    r = client.post("/api/chat", files={"image": (fake, img, "image/png")}).json()
    assert r["result"] == "NOT FOUND"
    assert "do not pay" in r["text"].lower()

    # Step 2: the planted HIGH booking shows its reasons and the fraud ring.
    high = client.get(f"/api/bookings/{demo['hero_high_booking_id']}").json()
    assert high["risk"]["band"] == "HIGH"
    rules = {x["rule_id"] for x in high["risk"]["reasons"]}
    assert {"new_shipper_high_value", "late_booking_risky_payment", "linked_to_flagged_shipper"} <= rules
    ring = client.get(f"/graph/shipper/{high['shipper']['id']}").json()
    assert ring["summary"]["connected_shippers"] >= 3 and ring["summary"]["flagged_shippers"] >= 2

    # Step 3: the clean booking gets a quote, cover is issued, the QR scans GENUINE.
    clean = client.get(f"/api/bookings/{demo['hero_clean_booking_id']}").json()
    assert clean["risk"]["band"] == "LOW" and clean["quote"]["eligible"]
    pol = client.post("/api/policies", json={"booking_id": demo["hero_clean_booking_id"]}).json()
    token = pol["verify_url"].rsplit("/", 1)[1]
    scan = client.get(f"/verify/policy/{token}").json()
    assert scan["result"] == "GENUINE"
    page = client.get(f"/verify/policy/{token}", headers={"accept": "text/html"})
    assert "GENUINE" in page.text and "<html" in page.text
    if ocr.qr_available():
        cert = client.get(pol["certificate_png"]).content
        r = client.post("/api/chat", files={"image": ("cert.png", cert, "image/png")}).json()
        assert r["result"] == "GENUINE"

    end = client.get("/api/stats").json()
    assert end["policies_issued"] == start["policies_issued"] + 1
    assert end["fakes_caught"] == start["fakes_caught"] + 1

    # Reset puts everything back for the next run.
    client.post("/api/admin/reset")
    assert client.get("/api/stats").json() == start
