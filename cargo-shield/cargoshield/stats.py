"""Numbers for the dashboard's top strip."""

from __future__ import annotations

import sqlite3
from typing import Any

from .verify import FAKE_RESULTS


def kpis(conn: sqlite3.Connection) -> dict[str, Any]:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    bands = {r["band"]: r["n"] for r in conn.execute("SELECT band, COUNT(*) AS n FROM risk_score GROUP BY band")}
    placeholders = ",".join("?" for _ in FAKE_RESULTS)
    return {
        "screened": one("SELECT COUNT(*) FROM risk_score"),
        "flagged_high": bands.get("HIGH", 0),
        "flagged_medium": bands.get("MEDIUM", 0),
        "review_pending": one("SELECT COUNT(*) FROM booking WHERE status IN ('in_review', 'on_hold')"),
        "policies_issued": one("SELECT COUNT(*) FROM policy WHERE status = 'active'"),
        "premium_written_usd": round(one("SELECT COALESCE(SUM(premium_usd), 0) FROM policy WHERE status = 'active'"), 2),
        "sum_insured_usd": round(one("SELECT COALESCE(SUM(sum_insured_usd), 0) FROM policy WHERE status = 'active'"), 2),
        "fakes_caught": one(f"SELECT COUNT(*) FROM verification_log WHERE result IN ({placeholders})", *FAKE_RESULTS),
        "checks_total": one("SELECT COUNT(*) FROM verification_log"),
    }
