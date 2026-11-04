"""How well the engine finds the planted risks.

This is the only module that reads the seed's ground truth (seed_truth), and
it only reads it to grade the engine. The engine itself never sees it.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any


def engine_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Grade the engine on the seeded bookings only. Live-feed and API bookings
    have no ground truth, so counting them as clean would skew the rates."""
    from . import db

    last_seeded = db.get_meta(conn, "seed_max_booking_id") or 10**12
    rows = conn.execute(
        """
        SELECT b.id, b.awb, r.band, r.score
        FROM booking b JOIN risk_score r ON r.booking_id = b.id
        WHERE b.id <= ?
        """,
        (last_seeded,),
    ).fetchall()
    truth: dict[int, list[str]] = defaultdict(list)
    for t in conn.execute("SELECT booking_id, pattern FROM seed_truth"):
        truth[t["booking_id"]].append(t["pattern"])

    planted_total = planted_caught = planted_high = 0
    clean_total = clean_med_plus = clean_high = 0
    by_pattern: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "caught": 0})
    missed: list[str] = []
    bands = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    for r in rows:
        bands[r["band"]] += 1
        flagged = r["band"] in ("MEDIUM", "HIGH")
        if r["id"] in truth:
            planted_total += 1
            planted_caught += flagged
            planted_high += r["band"] == "HIGH"
            for p in truth[r["id"]]:
                by_pattern[p]["total"] += 1
                by_pattern[p]["caught"] += flagged
            if not flagged:
                missed.append(r["awb"])
        else:
            clean_total += 1
            clean_med_plus += flagged
            clean_high += r["band"] == "HIGH"
    return {
        "bands": bands,
        "planted_total": planted_total,
        "planted_caught": planted_caught,
        "planted_high": planted_high,
        "clean_total": clean_total,
        "clean_flagged_medium_plus": clean_med_plus,
        "clean_flagged_high": clean_high,
        "fp_rate_medium_plus": clean_med_plus / clean_total if clean_total else 0.0,
        "fp_rate_high": clean_high / clean_total if clean_total else 0.0,
        "detection_rate": planted_caught / planted_total if planted_total else 0.0,
        "by_pattern": dict(by_pattern),
        "missed_awbs": missed,
    }


def format_report(rep: dict[str, Any]) -> str:
    lines = [
        "CargoShield engine check on the seeded demo data",
        f"  Bookings scored: {sum(rep['bands'].values())}  "
        f"(LOW {rep['bands']['LOW']}, MEDIUM {rep['bands']['MEDIUM']}, HIGH {rep['bands']['HIGH']})",
        f"  Planted risks caught (MEDIUM or HIGH): {rep['planted_caught']}/{rep['planted_total']} "
        f"({rep['detection_rate']:.1%}); {rep['planted_high']} of them HIGH",
        f"  False positives on clean bookings: {rep['clean_flagged_medium_plus']}/{rep['clean_total']} "
        f"= {rep['fp_rate_medium_plus']:.2%} at MEDIUM+, "
        f"{rep['clean_flagged_high']}/{rep['clean_total']} = {rep['fp_rate_high']:.2%} at HIGH",
        "  By planted pattern:",
    ]
    for p, v in sorted(rep["by_pattern"].items()):
        lines.append(f"    {p:<28} {v['caught']}/{v['total']}")
    if rep["missed_awbs"]:
        lines.append(f"  Missed: {', '.join(rep['missed_awbs'])}")
    return "\n".join(lines)
