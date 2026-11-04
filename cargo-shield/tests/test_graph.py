"""Milestone 3: the fraud graph."""

from cargoshield import db, graph


def _shipper_ids(sub):
    return {n["shipper_id"] for n in sub["nodes"] if n["kind"] == "shipper"}


def test_planted_ring_lights_up_from_any_member(conn):
    ring = set(db.get_meta(conn, "demo")["ring_shipper_ids"])
    assert 4 <= len(ring) <= 5
    for sid in ring:
        sub = graph.subgraph(conn, sid, hops=2)
        assert ring <= _shipper_ids(sub), sid
        assert sub["summary"]["flagged_shippers"] >= 2


def test_endpoint_returns_two_hop_neighbourhood(client, conn):
    ring = db.get_meta(conn, "demo")["ring_shipper_ids"]
    r = client.get(f"/graph/shipper/{ring[1]}")
    assert r.status_code == 200
    body = r.json()
    assert body["hops"] == 2
    assert {n["shipper_id"] for n in body["nodes"] if n["kind"] == "shipper"} >= set(ring)
    kinds = {n["kind"] for n in body["nodes"]}
    assert {"phone", "email", "payment_ref"} <= kinds
    assert client.get("/graph/shipper/999999").status_code == 404


def test_graph_never_exposes_raw_contact_details(client, conn):
    ring = db.get_meta(conn, "demo")["ring_shipper_ids"]
    text = client.get(f"/graph/shipper/{ring[0]}").text
    for r in conn.execute(f"SELECT phone, email FROM shipper WHERE id IN ({','.join('?' * len(ring))})", ring):
        assert r["phone"] not in text
        assert r["email"] not in text


def test_shared_agent_contact_alone_does_not_flag(conn):
    """Clean shippers who book through the same agent phone are linked but not flagged."""
    idx = graph.LinkIndex(conn)
    flagged = set(graph.flagged_shippers(conn))
    shared_phones = [ids for (t, _v), ids in idx.by_ident.items() if t == "phone" and len(ids) >= 3]
    clean_groups = [ids for ids in shared_phones if not (ids & flagged)]
    assert clean_groups, "expected at least one clean shared-agent group"
    for ids in clean_groups:
        for sid in ids:
            assert idx.linked_flagged(sid, flagged)["count"] == 0


def test_links_table_holds_every_shipper(conn):
    n = conn.execute("SELECT COUNT(DISTINCT a_value) FROM contact_link WHERE a_type = 'shipper'").fetchone()[0]
    assert n == conn.execute("SELECT COUNT(*) FROM shipper").fetchone()[0]


def test_mask():
    assert graph.mask("phone", "+254 0712 345 678") == "+254 ••• ••• 678"
    assert graph.mask("email", "orders@baobab-fresh.example") == "o•••@b•••.example"
    assert graph.mask("payment_ref", "ACC-12345678") == "ACC-••••78"
