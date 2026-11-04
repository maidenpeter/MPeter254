"""Milestone 4: cover pricing and policies."""

import pytest

from cargoshield import config, db, policies, pricing


def test_high_band_is_never_offered_cover():
    q = pricing.quote(500000, 10, "valuables", "HIGH")
    assert not q.eligible and "review" in q.reason.lower()


def test_sum_insured_is_declared_value_minus_carrier_liability():
    c = config.settings()["cover"]
    q = pricing.quote(68500, 420, "pharma", "LOW")
    liability = 420 * c["carrier_liability_sdr_per_kg"] * c["sdr_to_usd"]
    assert q.carrier_liability_usd == pytest.approx(liability, abs=0.01)
    assert q.sum_insured_usd == pytest.approx(68500 - liability, abs=0.01)


def test_premium_build_up_adds_up():
    c = config.settings()["cover"]
    q = pricing.quote(68500, 420, "pharma", "MEDIUM")
    assert q.eligible
    rate = config.commodities()["classes"]["pharma"]["base_loss_rate"]
    el = q.sum_insured_usd * rate * c["risk_multiplier"]["MEDIUM"]
    assert q.expected_loss_usd == pytest.approx(el, abs=0.01)
    gross = el / (1 - c["expense_loading"] - c["profit_margin"])
    assert q.gross_premium_usd == pytest.approx(gross, abs=0.01)
    # expected loss + expense + profit = gross premium (to the cent, allowing rounding)
    assert q.expected_loss_usd + q.expense_usd + q.profit_usd == pytest.approx(q.gross_premium_usd, abs=0.02)
    assert q.premium_usd == q.gross_premium_usd
    assert q.steps[-1]["value"] == q.premium_usd


def test_medium_costs_more_than_low():
    low = pricing.quote(68500, 420, "pharma", "LOW")
    med = pricing.quote(68500, 420, "pharma", "MEDIUM")
    assert med.premium_usd > low.premium_usd


def test_minimum_premium_floor():
    q = pricing.quote(5000, 100, "documents", "LOW")
    assert q.eligible and q.floor_applied
    assert q.premium_usd == config.settings()["cover"]["minimum_premium_usd"]


def test_no_top_up_when_liability_covers_the_value():
    q = pricing.quote(2000, 400, "perishables", "LOW")
    assert not q.eligible and "already covers" in q.reason


def test_issue_policy_creates_unguessable_token_and_is_idempotent(conn):
    clean = db.get_meta(conn, "demo")["hero_clean_booking_id"]
    p1 = policies.issue(conn, clean)
    p2 = policies.issue(conn, clean)
    assert p1["id"] == p2["id"]
    assert len(p1["qr_token"]) >= 32
    others = {r[0] for r in conn.execute("SELECT qr_token FROM policy WHERE id != ?", (p1["id"],))}
    assert p1["qr_token"] not in others
    png = policies.qr_png(policies.verify_url("http://localhost:8000", p1["qr_token"]))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_cannot_cover_a_high_booking(conn):
    high = db.get_meta(conn, "demo")["hero_high_booking_id"]
    with pytest.raises(policies.CoverError):
        policies.issue(conn, high)
