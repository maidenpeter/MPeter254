"""Milestone 2: the risk engine."""

import json
import shutil
from pathlib import Path

import pytest

from cargoshield import config, evaluation, risk
from cargoshield.features import booking_features


def test_every_planted_booking_is_medium_or_high(conn):
    rows = conn.execute(
        "SELECT b.awb, r.band, t.pattern FROM seed_truth t "
        "JOIN booking b ON b.id = t.booking_id JOIN risk_score r ON r.booking_id = b.id"
    ).fetchall()
    assert rows
    missed = [(r["awb"], r["pattern"]) for r in rows if r["band"] not in ("MEDIUM", "HIGH")]
    assert missed == []


def test_false_positive_rate_on_clean_bookings(conn, capsys):
    rep = evaluation.engine_report(conn)
    with capsys.disabled():
        print("\n" + evaluation.format_report(rep))
    assert rep["clean_total"] > 2500
    # Clean bookings flagged MEDIUM or HIGH. Reported above; keep it low.
    assert rep["fp_rate_medium_plus"] < 0.05
    assert rep["fp_rate_high"] < 0.01


def test_each_planted_pattern_fires_its_own_rule(conn):
    expected = {
        "new_shipper_high_value": {"new_shipper_high_value"},
        "vague_description": {"vague_description_dense", "vague_description_high_value"},
        "weight_volume_mismatch": {"weight_volume_mismatch", "implausible_weight"},
        "late_booking_risky_payment": {"late_booking_risky_payment"},
        "shared_contact_flagged": {"linked_to_flagged_shipper"},
    }
    rows = conn.execute(
        "SELECT t.pattern, r.rule_hits_json FROM seed_truth t JOIN risk_score r ON r.booking_id = t.booking_id"
    ).fetchall()
    for r in rows:
        ids = {h["id"] for h in json.loads(r["rule_hits_json"])}
        assert ids & expected[r["pattern"]], (r["pattern"], ids)


def test_flagged_bookings_have_plain_english_reasons(conn):
    for r in conn.execute("SELECT reasons_json FROM risk_score WHERE band != 'LOW'"):
        reasons = json.loads(r["reasons_json"])
        assert reasons and any(x["kind"] == "rule" for x in reasons)
        for x in reasons:
            assert x["text"].endswith(".") and "{" not in x["text"] and "?" not in x["text"]


def test_bands_and_routing(conn):
    for r in conn.execute("SELECT r.score, r.band, b.status FROM risk_score r JOIN booking b ON b.id = r.booking_id"):
        assert r["band"] == risk.band_for(r["score"])
        assert r["status"] == ("in_review" if r["band"] == "HIGH" else "accepted")
    # Nothing is auto-rejected.
    assert conn.execute("SELECT COUNT(*) FROM booking WHERE status = 'rejected'").fetchone()[0] == 0


def test_anomaly_model_alone_never_flags():
    res = risk.combine([], {"percentile": 1.0, "driver": "log_lead_time", "driver_label": "x"})
    assert res.score <= 39 and res.band == "LOW"
    assert res.anomaly_points == config.rules_config()["anomaly"]["max_points"]


def test_anomaly_points_only_for_the_unusual_tail():
    assert risk.anomaly_points(0.5) == 0
    assert risk.anomaly_points(0.9) == 0
    assert risk.anomaly_points(1.0) == 20
    assert 0 < risk.anomaly_points(0.97) < 20


def test_engine_never_reads_ground_truth():
    """No code (docstrings aside) in the engine touches the seed's answers."""
    import ast

    src = Path(risk.__file__).parent
    for name in ("risk.py", "features.py", "anomaly.py", "graph.py", "pricing.py", "verify.py", "messaging.py",
                 "policies.py", "simulate.py"):
        tree = ast.parse((src / name).read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                text = node.value
            elif isinstance(node, ast.Name):
                text = node.id
            elif isinstance(node, ast.Attribute):
                text = node.attr
            if text and name != "simulate.py":
                assert "is_planted_fraud" not in text, name
            if text:
                assert "seed_truth" not in text, name


def test_rules_are_tunable_from_yaml(tmp_path, conn, monkeypatch):
    """Changing a weight in rules.yaml changes the score with no code change."""
    cfg_dir = tmp_path / "config"
    shutil.copytree(config.config_dir(), cfg_dir)
    text = (cfg_dir / "rules.yaml").read_text()
    text = text.replace("  - id: kyc_pending\n    weight: 8", "  - id: kyc_pending\n    weight: 33")
    (cfg_dir / "rules.yaml").write_text(text)

    row = conn.execute(
        "SELECT b.*, s.created_at AS s_created_at, s.kyc_status AS s_kyc FROM booking b "
        "JOIN shipper s ON s.id = b.shipper_id WHERE s.kyc_status = 'pending' LIMIT 1"
    ).fetchone()
    f = booking_features(row, {"created_at": row["s_created_at"], "kyc_status": row["s_kyc"]})
    before = sum(h.points for h in risk.evaluate(risk.load_rules(), f))

    monkeypatch.setenv("CARGOSHIELD_CONFIG_DIR", str(cfg_dir))
    config.reload()
    try:
        after = sum(h.points for h in risk.evaluate(risk.load_rules(), f))
    finally:
        monkeypatch.delenv("CARGOSHIELD_CONFIG_DIR")
        config.reload()
    assert after - before == 25


def test_bad_rule_config_is_rejected():
    with pytest.raises(ValueError):
        risk.load_rules({"rules": [{"id": "x", "weight": 1, "when": {"field": "a", "op": "approx", "value": 1},
                                    "reason": "r"}]})


def test_screening_a_single_booking_matches_the_batch(conn, model):
    hero = json.loads(conn.execute("SELECT value FROM demo_meta WHERE key = 'demo'").fetchone()[0])
    before = risk.explain(conn, hero["hero_high_booking_id"])
    res = risk.screen_booking(conn, hero["hero_high_booking_id"], model)
    assert res.band == "HIGH" and res.score == before["score"]
    assert "linked_to_flagged_shipper" in res.rule_ids
