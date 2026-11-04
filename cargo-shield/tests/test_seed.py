"""Milestone 1: the synthetic data generator."""

import csv
import re
from datetime import datetime

from cargoshield import awb, config, db, fakes, seed


def test_counts_and_planted_share(conn, seeded):
    n_book = conn.execute("SELECT COUNT(*) FROM booking").fetchone()[0]
    n_ship = conn.execute("SELECT COUNT(*) FROM shipper").fetchone()[0]
    planted = conn.execute("SELECT COUNT(DISTINCT booking_id) FROM seed_truth").fetchone()[0]
    assert n_book == 3000
    assert 380 <= n_ship <= 420
    assert 0.03 <= planted / n_book <= 0.05


def test_every_planted_pattern_is_present(conn):
    patterns = {r[0] for r in conn.execute("SELECT DISTINCT pattern FROM seed_truth")}
    assert patterns == set(seed.PLANTED_PATTERNS)


def test_awbs_are_valid_unique_and_use_the_prefix(conn):
    rows = [r[0] for r in conn.execute("SELECT awb FROM booking")]
    assert len(rows) == len(set(rows))
    for a in rows:
        assert a.startswith(config.awb_prefix())
        assert awb.validate(a).ok, a


def test_bookings_follow_the_route_file(conn):
    routes = {}
    with open(config.routes_csv(), newline="") as fh:
        for r in csv.DictReader(fh):
            routes[(r["origin"], r["destination"])] = {seed.DAYS[d] for d in r["days_of_week"].split("|")}
    for r in conn.execute("SELECT origin, destination, flight_date FROM booking"):
        key = (r["origin"], r["destination"])
        assert key in routes, key
        assert datetime.fromisoformat(r["flight_date"]).weekday() in routes[key]


def test_bookings_are_never_in_the_future_and_lead_times_are_positive(conn):
    seeded_at = db.get_meta(conn, "seeded_at")
    late = conn.execute("SELECT COUNT(*) FROM booking WHERE booked_at > ?", (seeded_at,)).fetchone()[0]
    assert late == 0
    assert conn.execute("SELECT MIN(lead_time_hours) FROM booking").fetchone()[0] > 0


def test_bookings_happen_after_the_shipper_exists(conn):
    bad = conn.execute(
        "SELECT COUNT(*) FROM booking b JOIN shipper s ON s.id = b.shipper_id WHERE b.booked_at < s.created_at"
    ).fetchone()[0]
    assert bad == 0


def test_contact_details_cannot_reach_real_people(conn):
    for r in conn.execute("SELECT phone, email FROM shipper"):
        assert r["email"].endswith(".example"), r["email"]  # RFC 2606 reserved domain
        assert re.fullmatch(r"\+\d{2,3} 0\d{3} \d{3} \d{3}", r["phone"]), r["phone"]


def test_ground_truth_is_consistent(conn):
    # every booking from a planted shipper carries a planted pattern, and vice versa
    rows = conn.execute(
        "SELECT s.is_planted_fraud AS p, (SELECT COUNT(*) FROM seed_truth t WHERE t.booking_id = b.id) AS t "
        "FROM booking b JOIN shipper s ON s.id = b.shipper_id"
    ).fetchall()
    assert all(bool(r["p"]) == bool(r["t"]) for r in rows)


def test_fake_documents_exist_and_cannot_match_a_booking(conn):
    files = sorted(p.name for p in config.fakes_dir().iterdir() if p.suffix in (".png", ".jpg"))
    assert 5 <= len(files) <= 10
    assert set(files) == {f.filename for f in fakes.FAKES}
    for spec in fakes.FAKES:
        number = fakes.fake_awb(spec)
        exists = conn.execute("SELECT 1 FROM booking WHERE awb = ?", (number,)).fetchone()
        check = awb.validate(number)
        assert (not check.ok) or (exists is None), spec.filename
        assert check.ok == spec.valid_check_digit


def test_demo_heroes_are_recorded(conn):
    meta = db.get_meta(conn, "demo")
    assert awb.fmt(meta["hero_high_awb"]) == "999-60422180"
    assert awb.fmt(meta["hero_clean_awb"]) == "999-31509461"
    assert len(meta["ring_shipper_ids"]) == 5
