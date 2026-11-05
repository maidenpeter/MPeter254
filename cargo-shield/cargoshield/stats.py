"""Numbers for the dashboard's top strip."""

from __future__ import annotations

import sqlite3
from typing import Any

from .verify import DOCUMENT_INPUTS, FAKE_RESULTS


def kpis(conn: sqlite3.Connection) -> dict[str, Any]:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    bands = {r["band"]: r["n"] for r in conn.execute("SELECT band, COUNT(*) AS n FROM risk_score GROUP BY band")}
    placeholders = ",".join("?" for _ in FAKE_RESULTS)
    doc_inputs = ",".join("?" for _ in DOCUMENT_INPUTS)
    return {
        "screened": one("SELECT COUNT(*) FROM risk_score"),
        "flagged_high": bands.get("HIGH", 0),
        "flagged_medium": bands.get("MEDIUM", 0),
        "review_pending": one("SELECT COUNT(*) FROM booking WHERE status IN ('in_review', 'on_hold')"),
        "review_on_hold": one("SELECT COUNT(*) FROM booking WHERE status = 'on_hold'"),
        # Final decisions only (approved or refused); a hold is not a decision.
        "review_decided": one("SELECT COUNT(*) FROM booking b WHERE b.status IN ('accepted', 'rejected') AND EXISTS "
                              "(SELECT 1 FROM review_decision d WHERE d.booking_id = b.id)"),
        "policies_issued": one("SELECT COUNT(*) FROM policy WHERE status = 'active'"),
        "premium_written_usd": round(one("SELECT COALESCE(SUM(premium_usd), 0) FROM policy WHERE status = 'active'"), 2),
        "sum_insured_usd": round(one("SELECT COALESCE(SUM(sum_insured_usd), 0) FROM policy WHERE status = 'active'"), 2),
        # Documents, photos, QR codes and links that came back NOT FOUND or SUSPICIOUS.
        # Typed numbers don't count: a typo isn't a fake.
        "fakes_caught": one(f"SELECT COUNT(*) FROM verification_log WHERE result IN ({placeholders}) "
                            f"AND input_type IN ({doc_inputs})", *FAKE_RESULTS, *DOCUMENT_INPUTS),
        "checks_total": one("SELECT COUNT(*) FROM verification_log"),
    }
