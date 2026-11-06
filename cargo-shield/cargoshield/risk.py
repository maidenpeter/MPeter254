"""Risk engine (Milestone 2).

Two layers, combined into one score from 0 to 100:

1. Rules from config/rules.yaml. Each rule that fires adds its weight and a
   plain-English sentence.
2. The anomaly model (anomaly.py). Only the most unusual bookings earn a few
   supporting points. A booking with no rule hit is capped at the top of LOW,
   so the model alone never flags anyone.

Bands: LOW 0-39, MEDIUM 40-69, HIGH 70+. HIGH goes to the manual review
queue. Nothing is auto-rejected.

The engine reads only what the airline knows at booking time. It never reads
the seed's ground truth (shipper.is_planted_fraud or the seed_truth table).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

from . import config, db, graph
from .anomaly import AnomalyModel
from .features import booking_features

# ------------------------------------------------------------------ rules

_OPS = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


@dataclass(frozen=True)
class Rule:
    id: str
    weight: float
    when: Mapping[str, Any]
    reason: str


@dataclass(frozen=True)
class RuleHit:
    id: str
    points: float
    reason: str
    kind: str = "rule"


def load_rules(cfg: Mapping[str, Any] | None = None) -> list[Rule]:
    cfg = cfg or config.rules_config()
    rules = []
    seen = set()
    for r in cfg.get("rules") or []:
        if r["id"] in seen:
            raise ValueError(f"duplicate rule id {r['id']}")
        seen.add(r["id"])
        _validate_condition(r["when"], r["id"])
        rules.append(Rule(r["id"], float(r["weight"]), r["when"], r["reason"]))
    return rules


def _validate_condition(cond: Mapping[str, Any], rule_id: str) -> None:
    if "all" in cond or "any" in cond:
        for c in cond.get("all") or cond.get("any") or []:
            _validate_condition(c, rule_id)
        return
    if cond.get("op") not in _OPS or "field" not in cond:
        raise ValueError(f"rule {rule_id}: bad condition {cond}")


def _matches(cond: Mapping[str, Any], f: Mapping[str, Any]) -> bool:
    if "all" in cond:
        return all(_matches(c, f) for c in cond["all"])
    if "any" in cond:
        return any(_matches(c, f) for c in cond["any"])
    value = f.get(cond["field"])
    if value is None:
        return False
    try:
        return bool(_OPS[cond["op"]](value, cond["value"]))
    except TypeError:
        return False


class _Fill(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def render_reason(template: str, f: Mapping[str, Any]) -> str:
    try:
        return template.format_map(_Fill(f))
    except (ValueError, TypeError, KeyError):
        return template


def evaluate(rules: Sequence[Rule], f: Mapping[str, Any], skip: set[str] | frozenset = frozenset()) -> list[RuleHit]:
    return [
        RuleHit(r.id, r.weight, render_reason(r.reason, f))
        for r in rules
        if r.id not in skip and _matches(r.when, f)
    ]


# ------------------------------------------------------------------ scoring


def bands() -> dict[str, list[int]]:
    return config.rules_config()["bands"]


def band_for(score: int | float | None) -> str:
    s = int(score or 0)
    b = bands()
    if s >= b["HIGH"][0]:
        return "HIGH"
    if s >= b["MEDIUM"][0]:
        return "MEDIUM"
    return "LOW"


@dataclass
class RiskResult:
    score: int
    band: str
    reasons: list[dict[str, Any]]
    hits: list[dict[str, Any]]
    anomaly_percentile: float | None
    anomaly_points: int
    features: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def rule_ids(self) -> list[str]:
        return [h["id"] for h in self.hits if h["kind"] == "rule"]


def anomaly_points(percentile: float | None) -> int:
    """Supporting points from the model. Too small to report means zero, so the
    reasons shown always add up to the score (before the 100 cap)."""
    if percentile is None:
        return 0
    a = config.rules_config()["anomaly"]
    trig = float(a["trigger_percentile"])
    frac = (percentile - trig) / max(1.0 - trig, 1e-9)
    pts = int(round(float(a["max_points"]) * min(max(frac, 0.0), 1.0)))
    return pts if pts >= int(a["min_points_to_report"]) else 0


def combine(hits: Sequence[RuleHit], anomaly: Mapping[str, Any] | None) -> RiskResult:
    a = config.rules_config()["anomaly"]
    pct = anomaly["percentile"] if anomaly else None
    pts = anomaly_points(pct)

    ordered = sorted(hits, key=lambda h: -h.points)
    reasons = [{"rule_id": h.id, "text": h.reason, "points": int(h.points), "kind": "rule"} for h in ordered]
    hit_rows = [{"id": h.id, "points": int(h.points), "kind": "rule"} for h in ordered]

    total = sum(h.points for h in hits)
    if anomaly is not None:
        hit_rows.append({"id": "anomaly_model", "points": pts, "kind": "model",
                         "percentile": round(pct, 4), "driver": anomaly.get("driver")})
        if pts:
            how = ("among the most unusual 1% of past bookings" if pct >= 0.99
                   else f"more unusual than {pct:.0%} of past bookings")
            reasons.append({
                "rule_id": "anomaly_model",
                "text": (f"Unusual pattern: {how}, mostly because of the {anomaly['driver_label']}. "
                         "Supporting signal only."),
                "points": pts,
                "kind": "model",
            })
        total += pts
    if not hits:
        total = min(total, int(a["max_score_without_rule_hit"]))
    score = int(min(100, round(total)))
    return RiskResult(score, band_for(score), reasons, hit_rows, pct, pts)


def _shipper_of(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"created_at": row["s_created_at"], "kyc_status": row["s_kyc"]}


_BOOKING_SQL = (
    "SELECT b.*, s.created_at AS s_created_at, s.kyc_status AS s_kyc "
    "FROM booking b JOIN shipper s ON s.id = b.shipper_id"
)


def _write(conn: sqlite3.Connection, booking_id: int, res: RiskResult, when: str) -> None:
    conn.execute(
        """
        INSERT INTO risk_score (booking_id, score, band, reasons_json, rule_hits_json, anomaly_score, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (booking_id) DO UPDATE SET
            score = excluded.score, band = excluded.band, reasons_json = excluded.reasons_json,
            rule_hits_json = excluded.rule_hits_json, anomaly_score = excluded.anomaly_score,
            scored_at = excluded.scored_at
        """,
        (booking_id, res.score, res.band, json.dumps(res.reasons), json.dumps(res.hits),
         None if res.anomaly_percentile is None else round(res.anomaly_percentile, 4), when),
    )


def _set_status(conn: sqlite3.Connection, booking_id: int, band: str) -> None:
    """Route the booking unless a human has already decided on it."""
    decided = conn.execute("SELECT 1 FROM review_decision WHERE booking_id = ?", (booking_id,)).fetchone()
    if not decided:
        conn.execute("UPDATE booking SET status = ? WHERE id = ?",
                     ("in_review" if band == "HIGH" else "accepted", booking_id))


def score_all(conn: sqlite3.Connection, model: AnomalyModel) -> dict[str, Any]:
    """Score every booking in two passes (own evidence, then fraud graph)."""
    rcfg = config.rules_config()
    rules = load_rules(rcfg)
    graph_ids = set(rcfg.get("graph_rules") or [])
    high_min = int(rcfg["bands"]["HIGH"][0])

    rows = conn.execute(_BOOKING_SQL).fetchall()
    base_feats = [booking_features(r, _shipper_of(r), model.route_freq) for r in rows]
    anomalies = model.score(base_feats)

    # Pass 1: each booking on its own evidence.
    # Start from facts only (watchlist, reviewer rejections), never from old scores,
    # so rescoring after a rules.yaml change gives the same answer every time.
    flagged = set(graph.flagged_shippers(conn, include_scores=False))
    cleared = graph.approved_bookings(conn)  # a person reviewed these and let them fly
    high_own: dict[int, dict[int, str]] = {}
    for r, f, an in zip(rows, base_feats, anomalies):
        if r["id"] in cleared:
            continue
        if combine(evaluate(rules, f, skip=graph_ids), an).score >= high_min:
            flagged.add(r["shipper_id"])
            high_own.setdefault(r["shipper_id"], {})[r["id"]] = r["booked_at"]
    rejected = _rejected_by_shipper(conn)

    # Pass 2: add what the network and the shipper's other bookings say.
    idx = graph.LinkIndex(conn)
    when = db.iso(db.utcnow())
    counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    for r, an in zip(rows, anomalies):
        linked = idx.linked_flagged(r["shipper_id"], flagged)
        other = _other_flag(r["id"], r["booked_at"], high_own.get(r["shipper_id"], {}), rejected.get(r["shipper_id"], {}))
        f = booking_features(r, _shipper_of(r), model.route_freq, linked, other)
        res = combine(evaluate(rules, f), an)
        _write(conn, r["id"], res, when)
        _set_status(conn, r["id"], res.band)
        counts[res.band] += 1
    conn.commit()
    return {"bands": counts, "flagged_shippers": len(flagged), "scored": len(rows)}


def screen_booking(conn: sqlite3.Connection, booking_id: int, model: AnomalyModel) -> RiskResult:
    """Score one booking (a new one, or a rescore) against today's flags."""
    rules = load_rules()
    row = conn.execute(_BOOKING_SQL + " WHERE b.id = ?", (booking_id,)).fetchone()
    if row is None:
        raise KeyError(booking_id)
    flagged = set(graph.flagged_shippers(conn))
    linked = graph.LinkIndex(conn).linked_flagged(row["shipper_id"], flagged)
    other = _other_flag(booking_id, row["booked_at"], _high_own_from_scores(conn, row["shipper_id"]),
                        _rejected_by_shipper(conn).get(row["shipper_id"], {}))
    f = booking_features(row, _shipper_of(row), model.route_freq, linked, other)
    an = model.score([f])[0]
    res = combine(evaluate(rules, f), an)
    res.features = f
    _write(conn, booking_id, res, db.iso(db.utcnow()))
    _set_status(conn, booking_id, res.band)
    conn.commit()
    return res


def _rejected_by_shipper(conn: sqlite3.Connection) -> dict[int, dict[int, str]]:
    """shipper -> {refused booking id: when it was refused}."""
    out: dict[int, dict[int, str]] = {}
    for r in conn.execute(
        "SELECT b.shipper_id, b.id, d.decided_at FROM review_decision d JOIN booking b ON b.id = d.booking_id "
        "WHERE d.decision = 'reject'"
    ):
        out.setdefault(r["shipper_id"], {})[r["id"]] = r["decided_at"]
    return out


def _booked_at(conn: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT id, booked_at FROM booking WHERE id IN ({','.join('?' for _ in ids)})", sorted(ids))}


def _high_own_from_scores(conn: sqlite3.Connection, shipper_id: int, cleared: set[int] | None = None) -> dict[int, str]:
    """The shipper's bookings that score HIGH on their own evidence (stored scores),
    leaving out any a reviewer approved."""
    rcfg = config.rules_config()
    graph_ids = set(rcfg.get("graph_rules") or [])
    cap = int(rcfg["anomaly"]["max_score_without_rule_hit"])
    high_min = int(rcfg["bands"]["HIGH"][0])
    out: dict[int, str] = {}
    cleared = graph.approved_bookings(conn) if cleared is None else cleared
    for r in conn.execute(
        "SELECT b.id, b.booked_at, r.rule_hits_json FROM booking b JOIN risk_score r ON r.booking_id = b.id "
        "WHERE b.shipper_id = ?",
        (shipper_id,),
    ):
        if r["id"] not in cleared and graph.base_score(json.loads(r["rule_hits_json"]), graph_ids, cap) >= high_min:
            out[r["id"]] = r["booked_at"]
    return out


def _other_flag(booking_id: int, booked_at: str, high_own: dict[int, str], rejected: dict[int, str]) -> dict[str, bool]:
    """What the shipper's *other* bookings said when this one was booked: refusals
    decided before it, and HIGH bookings made before it. Never this booking itself,
    and never hindsight (a past booking isn't rescored with what came later)."""
    return {
        "rejected": any(bid != booking_id and when <= booked_at for bid, when in rejected.items()),
        "high": any(bid != booking_id and when <= booked_at for bid, when in high_own.items()),
    }


def rescore_bookings(conn: sqlite3.Connection, booking_ids: Sequence[int], model: AnomalyModel) -> int:
    """Rescore several bookings in one pass (context built once, not per booking)."""
    ids = list(dict.fromkeys(booking_ids))
    if not ids:
        return 0
    rules = load_rules()
    flagged = set(graph.flagged_shippers(conn))
    idx = graph.LinkIndex(conn)
    rejected = _rejected_by_shipper(conn)
    cleared = graph.approved_bookings(conn)
    rows = conn.execute(_BOOKING_SQL + f" WHERE b.id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    high_cache: dict[int, dict[int, str]] = {}
    feats = []
    for row in rows:
        sid = row["shipper_id"]
        if sid not in high_cache:
            high_cache[sid] = _high_own_from_scores(conn, sid, cleared)
        other = _other_flag(row["id"], row["booked_at"], high_cache[sid], rejected.get(sid, {}))
        feats.append(booking_features(row, _shipper_of(row), model.route_freq, idx.linked_flagged(sid, flagged), other))
    when = db.iso(db.utcnow())
    for row, f, an in zip(rows, feats, model.score(feats)):
        res = combine(evaluate(rules, f), an)
        _write(conn, row["id"], res, when)
        _set_status(conn, row["id"], res.band)
    conn.commit()
    return len(rows)


def rescore_after_flag(conn: sqlite3.Connection, shipper_id: int, model: AnomalyModel,
                       skip_booking: int | None = None) -> int:
    """A shipper's standing just changed: a booking scored HIGH, or a reviewer
    rejected or approved one.

    Rescore the open bookings that this affects: the shipper's own other
    bookings and those of every shipper sharing a phone, email or payer account
    with them. Only bookings nobody has decided on yet and whose flight is still
    to go. Returns how many were rescored.
    """
    idx = graph.LinkIndex(conn)
    shippers = {shipper_id} | set(idx.neighbours(shipper_id))
    now = db.utcnow()
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT b.id, b.booked_at, b.lead_time_hours FROM booking b WHERE b.shipper_id IN "
            f"({','.join('?' for _ in shippers)}) AND NOT EXISTS "
            f"(SELECT 1 FROM review_decision d WHERE d.booking_id = b.id)",
            sorted(shippers),
        ).fetchall()
        if r["id"] != skip_booking
        and db.parse_dt(r["booked_at"]) + timedelta(hours=r["lead_time_hours"]) > now
    ]
    return rescore_bookings(conn, ids, model)


def explain(conn: sqlite3.Connection, booking_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM risk_score WHERE booking_id = ?", (booking_id,)).fetchone()
    if r is None:
        return None
    return {
        "score": r["score"],
        "band": r["band"],
        "reasons": json.loads(r["reasons_json"]),
        "hits": json.loads(r["rule_hits_json"]),
        "anomaly_percentile": r["anomaly_score"],
        "scored_at": r["scored_at"],
    }
