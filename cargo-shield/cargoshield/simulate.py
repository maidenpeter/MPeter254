"""Live booking feed for the demo.

Creates one new booking "now", screens it, and returns its id. Most are
ordinary; about one in eight carries a risk pattern, so the presenter can
show the engine catching something live.
"""

from __future__ import annotations

import random
import sqlite3
from collections import Counter
from datetime import timedelta
from typing import Any

from . import commodity, db, graph, risk
from .anomaly import AnomalyModel
from .seed import COUNTRIES, Generator


def _generator(conn: sqlite3.Connection, rng: random.Random) -> Generator:
    g = Generator(seed=rng.randrange(1 << 30), anchor=db.utcnow(), history_days=1)
    g.used_serials |= {r[0][3:10] for r in conn.execute("SELECT awb FROM booking")}
    g.used_names |= {r[0] for r in conn.execute("SELECT name FROM shipper")}
    g.used_phones |= {r[0] for r in conn.execute("SELECT phone FROM shipper")}
    g.used_emails |= {r[0] for r in conn.execute("SELECT email FROM shipper")}
    return g


def _as_generator_shipper(conn: sqlite3.Connection, row: sqlite3.Row, g: Generator) -> dict[str, Any]:
    classes = Counter(r[0] for r in conn.execute("SELECT commodity_class FROM booking WHERE shipper_id = ?", (row["id"],)))
    refs = Counter(r[0] for r in conn.execute(
        "SELECT payment_ref FROM booking WHERE shipper_id = ? AND payment_ref IS NOT NULL", (row["id"],)))
    age_days = (g.anchor - db.parse_dt(row["created_at"])).days
    return {
        "id": row["id"], "name": row["name"], "country": row["country"], "phone": row["phone"],
        "email": row["email"], "created_at": row["created_at"], "kyc_status": row["kyc_status"],
        "_cls": classes.most_common(1)[0][0] if classes else "general_cargo",
        "_hub": COUNTRIES.get(row["country"], ("", "NBO"))[1],
        "_payer": refs.most_common(1)[0][0] if refs else g.payer_ref("ACC"),
        "_credit": refs.most_common(1)[0][0] if refs else None,
        "_new": age_days < 150,
    }


def _insert_shipper(conn: sqlite3.Connection, s: dict[str, Any]) -> int:
    cols = ["name", "country", "phone", "email", "created_at", "kyc_status", "is_planted_fraud"]
    cur = conn.execute(f"INSERT INTO shipper ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                       [s[c] for c in cols])
    sid = int(cur.lastrowid)
    conn.execute("INSERT INTO contact_link (a_type, a_value, b_type, b_value) VALUES ('shipper', ?, 'phone', ?) "
                 "ON CONFLICT (a_type, a_value, b_type, b_value) DO NOTHING", (str(sid), s["phone"]))
    conn.execute("INSERT INTO contact_link (a_type, a_value, b_type, b_value) VALUES ('shipper', ?, 'email', ?) "
                 "ON CONFLICT (a_type, a_value, b_type, b_value) DO NOTHING", (str(sid), s["email"].lower()))
    return sid


def _now_timing(g: Generator, route, lead_hours: float):
    booked = g.anchor
    dep = route.next_departure(booked + timedelta(hours=lead_hours))
    return booked, dep


def simulate_booking(conn: sqlite3.Connection, model: AnomalyModel, rng: random.Random | None = None,
                     risky: bool | None = None) -> int:
    rng = rng or random.Random()
    g = _generator(conn, rng)
    risky = (rng.random() < 0.12) if risky is None else risky

    if not risky:
        row = conn.execute("SELECT * FROM shipper WHERE kyc_status != 'failed' ORDER BY RANDOM() LIMIT 1").fetchone()
        s = _as_generator_shipper(conn, row, g)
        b = g.clean_booking(s)
        route = next(r for r in g.routes if r.origin == b["origin"] and r.destination == b["destination"])
        booked, dep = _now_timing(g, route, rng.lognormvariate(3.9, 0.6))
        b.update(booked_at=db.iso(booked), flight_date=route.local_date(dep).isoformat(),
                 lead_time_hours=round((dep - booked).total_seconds() / 3600, 1))
        b["shipper_id"] = s["id"]
    else:
        pattern = rng.choice(["new_high_value", "late_cash", "vague_dense"])
        soon = [(r.next_departure(g.anchor + timedelta(hours=3)), r) for r in g.routes]
        soon = [(d, r) for d, r in soon if d <= g.anchor + timedelta(hours=11)]
        if pattern == "late_cash" and not soon:
            pattern = "vague_dense"  # nothing departs in the next 11 hours
        if pattern == "new_high_value":
            cls = rng.choice(["valuables", "mobile_phones"])
            country = "HK" if cls == "mobile_phones" else rng.choice(["KE", "UG", "TZ"])
            s = g.make_shipper(country, cls, g.anchor - timedelta(days=rng.uniform(2, 20)), "pending",
                               personal_email=True)
            s["id"] = _insert_shipper(conn, s)
            text = rng.choice(commodity.info(cls)["descriptions"])
            phys = (g._physical(cls, weight=rng.uniform(3, 20), vpk=g.valuables_vpk(text)) if cls == "valuables"
                    else g._physical(cls, weight=rng.uniform(80, 400), vpk=rng.uniform(400, 1100)))
            route = g.route_from(s["_hub"], ["HKG", "JNB", "NBO"])
            booked, dep = _now_timing(g, route, rng.uniform(20, 60))
            b = g.booking(s, cls, text, route, phys,
                          rng.choice(["mobile_money", "card"]), booked, dep)
        else:
            row = conn.execute("SELECT * FROM shipper WHERE kyc_status != 'failed' ORDER BY RANDOM() LIMIT 1").fetchone()
            s = _as_generator_shipper(conn, row, g)
            route = g.route_from(s["_hub"])
            if pattern == "late_cash":
                cls = s["_cls"] if s["_cls"] != "valuables" else "general_cargo"
                dep, route = rng.choice(soon)
                b = g.booking(s, cls, rng.choice(commodity.info(cls)["descriptions"]), route, g._physical(cls),
                              rng.choice(["cash", "third_party"]), g.anchor, dep)
            else:
                booked, dep = _now_timing(g, route, rng.uniform(24, 90))
                phys = g._physical("general_cargo", weight=rng.uniform(200, 700), density=rng.uniform(800, 1500),
                                   vpk=rng.uniform(5, 25))
                b = g.booking(s, "general_cargo", rng.choice(["General goods", "Assorted items", "Various goods"]),
                              route, phys, rng.choice(["bank_transfer", "mobile_money"]), booked, dep)
        b["shipper_id"] = s["id"]

    cols = ["awb", "shipper_id", "origin", "destination", "flight_date", "commodity_text", "commodity_class",
            "pieces", "weight_kg", "volume_m3", "declared_value_usd", "payment_method", "payment_ref",
            "booked_at", "lead_time_hours", "status"]
    b["status"] = "new"
    cur = conn.execute(f"INSERT INTO booking ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                       [b[c] for c in cols])
    bid = int(cur.lastrowid)
    graph.add_links_for_booking(conn, b["shipper_id"], b["payment_ref"])
    conn.commit()
    risk.screen_booking(conn, bid, model)
    return bid
