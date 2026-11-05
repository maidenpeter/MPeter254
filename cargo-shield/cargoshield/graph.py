"""Fraud graph (Milestone 3).

Shippers are linked through identifiers they share: phone numbers, email
addresses and payer account references. Links are stored as a generic edge
list in contact_link:

    (shipper, "17") -> (phone, "+254 0712 345 678")

One "hop" is shipper -> shared identifier -> shipper.

A shipper counts as *flagged* when any of these is true:
  * KYC failed (internal watchlist)
  * a reviewer rejected one of their bookings
  * one of their bookings scores HIGH on its own evidence, i.e. without the
    graph rule (so flags don't feed on themselves)

Flags are internal. Nothing here is ever shown to the public.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict, deque
from typing import Any, Iterable

ID_TYPES = ("phone", "email", "payment_ref")
ID_LABELS = {"phone": "a phone number", "email": "an email address", "payment_ref": "a payer account"}


def rebuild_links(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM contact_link")
    rows: list[tuple[str, str, str, str]] = []
    for s in conn.execute("SELECT id, phone, email FROM shipper"):
        rows.append(("shipper", str(s["id"]), "phone", s["phone"]))
        rows.append(("shipper", str(s["id"]), "email", s["email"].lower()))
    for b in conn.execute("SELECT DISTINCT shipper_id, payment_ref FROM booking WHERE payment_ref IS NOT NULL"):
        rows.append(("shipper", str(b["shipper_id"]), "payment_ref", b["payment_ref"]))
    conn.executemany(
        "INSERT INTO contact_link (a_type, a_value, b_type, b_value) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (a_type, a_value, b_type, b_value) DO NOTHING",
        rows,
    )
    conn.commit()
    return len(rows)


def add_links_for_booking(conn: sqlite3.Connection, shipper_id: int, payment_ref: str | None) -> None:
    if payment_ref:
        conn.execute(
            "INSERT INTO contact_link (a_type, a_value, b_type, b_value) VALUES ('shipper', ?, 'payment_ref', ?) "
            "ON CONFLICT (a_type, a_value, b_type, b_value) DO NOTHING",
            (str(shipper_id), payment_ref),
        )


class LinkIndex:
    """In-memory view of contact_link for fast lookups."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_shipper: dict[int, set[tuple[str, str]]] = defaultdict(set)
        self.by_ident: dict[tuple[str, str], set[int]] = defaultdict(set)
        for r in conn.execute("SELECT a_value, b_type, b_value FROM contact_link WHERE a_type = 'shipper'"):
            sid = int(r["a_value"])
            ident = (r["b_type"], r["b_value"])
            self.by_shipper[sid].add(ident)
            self.by_ident[ident].add(sid)

    def neighbours(self, shipper_id: int) -> dict[int, set[tuple[str, str]]]:
        """Shippers one hop away, with the identifiers they share."""
        out: dict[int, set[tuple[str, str]]] = defaultdict(set)
        for ident in self.by_shipper.get(shipper_id, ()):
            for other in self.by_ident[ident]:
                if other != shipper_id:
                    out[other].add(ident)
        return out

    def linked_flagged(self, shipper_id: int, flagged: set[int]) -> dict[str, Any]:
        hits = {o: ids for o, ids in self.neighbours(shipper_id).items() if o in flagged}
        types = sorted({t for ids in hits.values() for t, _ in ids}, key=ID_TYPES.index)
        return {"count": len(hits), "via": [ID_LABELS[t] for t in types], "shipper_ids": sorted(hits)}


def base_score(rule_hits: Iterable[dict[str, Any]], graph_rule_ids: set[str], cap_without_rule: int) -> int:
    """Score from a booking's own evidence: everything except graph rules."""
    rule_pts = 0
    model_pts = 0
    any_rule = False
    for h in rule_hits:
        if h.get("kind") == "model":
            model_pts += h["points"]
        elif h["id"] not in graph_rule_ids:
            rule_pts += h["points"]
            any_rule = True
    total = rule_pts + model_pts
    if not any_rule:
        total = min(total, cap_without_rule)
    return min(100, int(round(total)))


def approved_bookings(conn: sqlite3.Connection) -> set[int]:
    """Bookings whose latest review decision is 'approve': a person cleared them."""
    return {
        r["booking_id"]
        for r in conn.execute(
            "SELECT d.booking_id, d.decision FROM review_decision d "
            "WHERE d.id = (SELECT MAX(d2.id) FROM review_decision d2 WHERE d2.booking_id = d.booking_id)"
        )
        if r["decision"] == "approve"
    }


def flagged_shippers(conn: sqlite3.Connection, include_scores: bool = True) -> dict[int, str]:
    """shipper_id -> reason they are flagged (internal only).

    include_scores=False leaves out flags that come from stored risk scores; a
    full rescore uses that so a rule change never starts from old scores.
    """
    from . import config

    rcfg = config.rules_config()
    graph_ids = set(rcfg.get("graph_rules") or [])
    cap = int(rcfg["anomaly"]["max_score_without_rule_hit"])
    high_min = int(rcfg["bands"]["HIGH"][0])

    flagged: dict[int, str] = {}
    for r in conn.execute("SELECT id FROM shipper WHERE kyc_status = 'failed'"):
        flagged[r["id"]] = "On the internal watchlist (KYC failed)"
    for r in conn.execute(
        "SELECT DISTINCT b.shipper_id FROM review_decision d JOIN booking b ON b.id = d.booking_id "
        "WHERE d.decision = 'reject'"
    ):
        flagged.setdefault(r["shipper_id"], "A booking was rejected after review")
    if not include_scores:
        return flagged
    cleared = approved_bookings(conn)
    for r in conn.execute(
        "SELECT b.id, b.shipper_id, r.rule_hits_json FROM risk_score r JOIN booking b ON b.id = r.booking_id "
        "WHERE r.score >= ?",
        (high_min,),
    ):
        if r["shipper_id"] in flagged or r["id"] in cleared:
            continue
        if base_score(json.loads(r["rule_hits_json"]), graph_ids, cap) >= high_min:
            flagged[r["shipper_id"]] = "Has a HIGH-risk booking on its own evidence"
    return flagged


# ------------------------------------------------------------------ display


def mask(id_type: str, value: str) -> str:
    """Show enough to see that two values match, not enough to contact anyone."""
    if id_type == "phone":
        digits = [c for c in value if c.isdigit()]
        cc = value.split(" ")[0] if " " in value else value[:4]
        return f"{cc} ••• ••• {''.join(digits[-3:])}"
    if id_type == "email":
        local, _, domain = value.partition("@")
        return f"{local[:1]}•••@{domain[:1]}•••{domain[domain.rfind('.'):] if '.' in domain else ''}"
    return f"{value[:4]}••••{value[-2:]}"


def subgraph(conn: sqlite3.Connection, shipper_id: int, hops: int = 2) -> dict[str, Any]:
    """Connected shippers within `hops` shipper-to-shipper hops, plus the identifiers linking them."""
    idx = LinkIndex(conn)
    if not conn.execute("SELECT 1 FROM shipper WHERE id = ?", (shipper_id,)).fetchone():
        raise KeyError(shipper_id)

    dist = {shipper_id: 0}
    q = deque([shipper_id])
    while q:
        cur = q.popleft()
        if dist[cur] >= hops:
            continue
        for other in idx.neighbours(cur):
            if other not in dist:
                dist[other] = dist[cur] + 1
                q.append(other)

    flagged = flagged_shippers(conn)
    ids = sorted(dist)
    placeholders = ",".join("?" for _ in ids)
    info = {
        r["id"]: r
        for r in conn.execute(
            f"""
            SELECT s.id, s.name, s.country, s.kyc_status,
                   COUNT(b.id) AS bookings,
                   MAX(r.score) AS max_score
            FROM shipper s
            LEFT JOIN booking b ON b.shipper_id = s.id
            LEFT JOIN risk_score r ON r.booking_id = b.id
            WHERE s.id IN ({placeholders})
            GROUP BY s.id, s.name, s.country, s.kyc_status
            """,
            ids,
        )
    }
    from . import risk

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for sid in ids:
        r = info[sid]
        nodes.append({
            "id": f"shipper:{sid}",
            "kind": "shipper",
            "shipper_id": sid,
            "label": r["name"],
            "country": r["country"],
            "kyc_status": r["kyc_status"],
            "bookings": r["bookings"],
            "max_score": r["max_score"],
            "max_band": risk.band_for(r["max_score"]) if r["max_score"] is not None else None,
            "hops": dist[sid],
            "flagged": sid in flagged,
            "flag_reason": flagged.get(sid),
            "is_center": sid == shipper_id,
        })
    # Identifiers shared by at least two shippers in view. Node ids are opaque
    # counters, never derived from the value, so the masked labels can't be undone.
    ident_ids: dict[tuple[str, str], str] = {}
    for sid in ids:
        for (t, v) in sorted(idx.by_shipper.get(sid, ())):
            members = [m for m in idx.by_ident[(t, v)] if m in dist]
            if len(members) < 2:
                continue
            if (t, v) not in ident_ids:
                ident_ids[(t, v)] = f"{t}:{len(ident_ids) + 1}"
                nodes.append({"id": ident_ids[(t, v)], "kind": t, "label": mask(t, v), "shared_by": len(members)})
            edges.append({"from": f"shipper:{sid}", "to": ident_ids[(t, v)], "kind": t})

    shippers = [n for n in nodes if n["kind"] == "shipper"]
    return {
        "center": shipper_id,
        "hops": hops,
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "connected_shippers": len(shippers) - 1,
            "flagged_shippers": sum(1 for n in shippers if n["flagged"]),
            "flagged_connected": sum(1 for n in shippers if n["flagged"] and not n["is_center"]),
            "center_flagged": shipper_id in flagged,
            "shared_identifiers": len(ident_ids),
        },
    }
