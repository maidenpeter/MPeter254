"""CargoShield web app: API and dashboard in one FastAPI process."""

from __future__ import annotations

import html
import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import timedelta
from typing import Any, Iterator, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import anomaly, awb as awbmod, commodity, config, db, evaluation, fakes, graph, messaging, ocr, policies, risk
from . import simulate, stats, verify

STATIC = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model = None
    # Remember the files' current versions, so the first later change is noticed.
    for path in _watched_files():
        _changed(path)
    try:
        app.state.model = anomaly.load()
    except FileNotFoundError:
        pass  # endpoints that need it explain how to seed
    yield


app = FastAPI(title="CargoShield demo", version="0.1.0", lifespan=lifespan,
              description="Trust and risk layer for a cargo airline. Demo data only.")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Paths anyone may reach. Everything else is the staff dashboard and its API.
PUBLIC_PREFIXES = ("/verify/",)
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}


@app.middleware("http")
async def guard(request: Request, call_next):
    """Two cheap protections for a demo laptop on shared Wi-Fi.

    1. Staff pages only for the presenter's own machine. A phone on the same
       network (HOST=0.0.0.0, for scanning QR codes) can reach /verify/... and
       nothing else. "Own machine" means loopback, or the same address the
       server answered on (the laptop opening its own LAN address).
    2. Writes must not come from another website (no cross-site form POSTs).
    """
    client = request.client.host if request.client else ""
    server = (request.scope.get("server") or ("",))[0]
    # A browser tricked by DNS rebinding sends the attacker's name as Host: refuse
    # any Host that isn't this machine or the configured public address.
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in _allowed_hosts(server):
        return JSONResponse({"detail": "Unknown host."}, status_code=400)
    local = client in LOCAL_CLIENTS or (bool(client) and client == server)  # unknown client: not local
    if not request.url.path.startswith(PUBLIC_PREFIXES) and not local:
        return JSONResponse({"detail": "Staff pages are only available on the presenter's laptop."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if size > ocr.MAX_UPLOAD_BYTES + 1_000_000:
            return JSONResponse({"detail": "Upload too large (8 MB at most)."}, status_code=413)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    return await call_next(request)


def _data_label() -> str:
    return str(config.settings()["airline"].get("data_label") or "")


def _allowed_hosts(server: str) -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    if server:
        hosts.add(server.lower())
    configured = str(config.settings()["server"].get("public_base_url") or "auto")
    if configured != "auto" and urlparse(configured).hostname:
        hosts.add(urlparse(configured).hostname.lower())
    return hosts


_watched: dict[str, float] = {}


def _changed(path: Path) -> bool:
    try:
        m = path.stat().st_mtime
    except FileNotFoundError:
        return False
    old = _watched.get(str(path))
    _watched[str(path)] = m
    return old is not None and old != m


# ------------------------------------------------------------------ plumbing


def _watched_files() -> list[Path]:
    return [config.config_dir() / n for n in ("rules.yaml", "settings.yaml", "commodities.yaml")] + [config.model_path()]


def _refresh_config() -> None:
    """Pick up edits to the YAML config (e.g. after `make train`) without a restart."""
    if any([_changed(config.config_dir() / n) for n in ("rules.yaml", "settings.yaml", "commodities.yaml")]):
        config.reload()


def get_conn() -> Iterator[sqlite3.Connection]:
    _refresh_config()
    if not db.is_seeded():
        raise HTTPException(503, "No demo database yet. Run `make seed` (or `python -m cargoshield.seed`).")
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_model(request: Request) -> anomaly.AnomalyModel:
    # Pick up a new model from `make train` without a restart.
    if _changed(config.model_path()):
        request.app.state.model = None
    if request.app.state.model is None:
        try:
            request.app.state.model = anomaly.load()
        except FileNotFoundError:
            raise HTTPException(503, "No anomaly model yet. Run `make seed` first.")
    return request.app.state.model


def base_url(request: Request) -> str:
    configured = str(config.settings()["server"].get("public_base_url") or "auto")
    return str(request.base_url).rstrip("/") if configured == "auto" else configured.rstrip("/")


def trusted_hosts(request: Request) -> set[str]:
    hosts = verify.default_trusted_hosts()
    if request.url.hostname:
        hosts.add(request.url.hostname.lower())
    return hosts


def wants_html(request: Request) -> bool:
    fmt = request.query_params.get("format")
    if fmt:
        return fmt == "html"
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


# ------------------------------------------------------------------ dashboard


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "database": config.db_path().exists(),
        "model": config.model_path().exists(),
        "ocr": ocr.tesseract_available(),
        "qr": ocr.qr_available(),
    }


@app.get("/api/stats")
def api_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return stats.kpis(conn)


@app.get("/api/demo")
def api_demo(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    meta = db.get_meta(conn, "demo", {})
    report = evaluation.engine_report(conn)
    report.pop("missed_awbs", None)
    return {
        "airline": config.airline_name(),
        "data_label": _data_label(),
        "seeded_at": db.get_meta(conn, "seeded_at"),
        "hero_high_booking_id": meta.get("hero_high_booking_id"),
        "hero_high_awb": awbmod.fmt(meta.get("hero_high_awb", "")),
        "hero_clean_booking_id": meta.get("hero_clean_booking_id"),
        "hero_clean_awb": awbmod.fmt(meta.get("hero_clean_awb", "")),
        "demo_fake": meta.get("demo_fake"),
        "engine_check": report,
        "ocr": ocr.tesseract_available(),
        "qr": ocr.qr_available(),
    }


_BOOKING_LIST_SQL = """
SELECT b.id, b.awb, b.origin, b.destination, b.flight_date, b.commodity_text, b.commodity_class,
       b.weight_kg, b.declared_value_usd, b.booked_at, b.lead_time_hours, b.status,
       r.score, r.band, r.reasons_json,
       (SELECT COUNT(*) FROM policy p WHERE p.booking_id = b.id AND p.status = 'active') AS policies
FROM booking b JOIN risk_score r ON r.booking_id = b.id
"""


def _list_item(r: sqlite3.Row) -> dict[str, Any]:
    reasons = json.loads(r["reasons_json"])
    departure = db.parse_dt(r["booked_at"]) + timedelta(hours=r["lead_time_hours"])
    top = next((x["text"] for x in reasons if x["kind"] == "rule"), None)
    return {
        "id": r["id"], "awb": awbmod.fmt(r["awb"]), "origin": r["origin"], "destination": r["destination"],
        "flight_date": r["flight_date"], "commodity_text": r["commodity_text"],
        "commodity_label": commodity.label(r["commodity_class"]), "weight_kg": r["weight_kg"],
        "declared_value_usd": r["declared_value_usd"], "booked_at": r["booked_at"], "status": r["status"],
        "score": r["score"], "band": r["band"], "top_reason": top, "reason_count": len(reasons),
        "has_policy": bool(r["policies"]),
        "departure_at": db.iso(departure),
    }


@app.get("/api/bookings")
def api_bookings(
    conn: sqlite3.Connection = Depends(get_conn),
    band: Literal["LOW", "MEDIUM", "HIGH", "FLAGGED"] | None = None,
    q: str | None = Query(None, max_length=60),
    sort: Literal["recent", "score"] = "recent",
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where, args = [], []
    if band == "FLAGGED":
        where.append("r.band IN ('MEDIUM', 'HIGH')")
    elif band:
        where.append("r.band = ?")
        args.append(band)
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        like = f"%{q.strip()}%"
        clause = "(b.commodity_text LIKE ? OR b.origin LIKE ? OR b.destination LIKE ?"
        args += [like, like, like]
        if digits:
            clause += " OR b.awb LIKE ?"
            args.append(f"%{digits}%")
        where.append(clause + ")")
    sql = _BOOKING_LIST_SQL + (" WHERE " + " AND ".join(where) if where else "")
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    order = "r.score DESC, b.booked_at DESC" if sort == "score" else "b.booked_at DESC"
    rows = conn.execute(f"{sql} ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"total": total, "items": [_list_item(r) for r in rows]}


def _booking_detail(conn: sqlite3.Connection, booking_id: int, request: Request) -> dict[str, Any]:
    b = conn.execute("SELECT * FROM booking WHERE id = ?", (booking_id,)).fetchone()
    if b is None:
        raise HTTPException(404, "No such booking")
    s = conn.execute("SELECT * FROM shipper WHERE id = ?", (b["shipper_id"],)).fetchone()
    flagged = graph.flagged_shippers(conn)
    age_days = (db.parse_dt(b["booked_at"]) - db.parse_dt(s["created_at"])).days
    nb = conn.execute("SELECT COUNT(*) FROM booking WHERE shipper_id = ?", (s["id"],)).fetchone()[0]
    p = policies.for_booking(conn, booking_id)
    q = policies.quote_booking(conn, booking_id, flagged=set(flagged))
    reviews = [dict(r) for r in conn.execute(
        "SELECT decision, note, decided_by, decided_at FROM review_decision WHERE booking_id = ? ORDER BY id", (booking_id,))]
    volume = b["volume_m3"]
    return {
        "booking": {
            **dict(b),
            "awb": awbmod.fmt(b["awb"]),
            "commodity_label": commodity.label(b["commodity_class"]),
            "payment_label": commodity.payment_label(b["payment_method"]),
            "density_kg_m3": round(b["weight_kg"] / volume) if volume else None,
            "departure_at": db.iso(db.parse_dt(b["booked_at"]) + timedelta(hours=b["lead_time_hours"])),
            "payment_ref": graph.mask("payment_ref", b["payment_ref"]) if b["payment_ref"] else None,
        },
        "shipper": {  # internal staff view; contact details masked
            "id": s["id"], "name": s["name"], "country": s["country"], "kyc_status": s["kyc_status"],
            "account_age_days_at_booking": age_days, "created_at": s["created_at"],
            "phone": graph.mask("phone", s["phone"]), "email": graph.mask("email", s["email"]),
            "bookings": nb, "flagged": s["id"] in flagged, "flag_reason": flagged.get(s["id"]),
        },
        "risk": risk.explain(conn, booking_id),
        "quote": q.to_dict(),
        "policy": policies.to_dict(p, base_url(request)) if p else None,
        "reviews": reviews,
    }


@app.get("/api/bookings/{booking_id}")
def api_booking(booking_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _booking_detail(conn, booking_id, request)


class NewBooking(BaseModel):
    shipper_id: int
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    flight_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                    description="Optional; must match now + lead_time_hours at the origin")
    commodity_text: str = Field(min_length=2, max_length=120)
    pieces: int = Field(ge=1, le=10000)
    weight_kg: float = Field(gt=0, le=150000)
    volume_m3: float = Field(gt=0, le=1000)
    declared_value_usd: float = Field(ge=0, le=1e9)
    payment_method: Literal["credit_account", "bank_transfer", "card", "mobile_money", "cash", "third_party"]
    payment_ref: str | None = Field(None, max_length=60)
    lead_time_hours: float = Field(ge=0, le=24 * 365)
    awb: str | None = None


@app.post("/api/bookings", status_code=201)
def api_new_booking(body: NewBooking, request: Request, conn: sqlite3.Connection = Depends(get_conn),
                    model: anomaly.AnomalyModel = Depends(get_model)) -> dict[str, Any]:
    """Screen a new booking: store it, score it, route it (HIGH -> review queue)."""
    if not conn.execute("SELECT 1 FROM shipper WHERE id = ?", (body.shipper_id,)).fetchone():
        raise HTTPException(422, "Unknown shipper_id")
    if body.awb:
        chk = awbmod.validate(body.awb)
        if not chk.ok:
            raise HTTPException(422, f"AWB {body.awb} is not valid ({chk.status})")
        if chk.prefix != config.awb_prefix():
            raise HTTPException(422, f"AWB prefix must be {config.awb_prefix()}")
        if chk.awb[3:10] in fakes.RESERVED_SERIALS:
            raise HTTPException(422, "That AWB number is reserved for the demo's fake documents")
        number = chk.awb
    else:
        used = {r[0][3:10] for r in conn.execute("SELECT awb FROM booking")} | fakes.RESERVED_SERIALS
        while True:
            serial = f"{secrets.randbelow(9_000_000) + 1_000_000}"
            if serial not in used:
                break
        number = awbmod.make(config.awb_prefix(), serial)
    if conn.execute("SELECT 1 FROM booking WHERE awb = ?", (number,)).fetchone():
        raise HTTPException(409, "AWB already exists")
    # The flight date is where the lead time lands, in the origin's local time.
    from datetime import date as _date, timedelta as _td, timezone as _tz

    from .seed import origin_zone

    now = db.utcnow()
    departure_local = (now + _td(hours=body.lead_time_hours)).replace(tzinfo=_tz.utc).astimezone(
        origin_zone(body.origin.upper()))
    flight_date = departure_local.date()
    if body.flight_date:
        try:
            given = _date.fromisoformat(body.flight_date)
        except ValueError:
            raise HTTPException(422, "flight_date is not a real date")
        if abs((given - flight_date).days) > 1:
            raise HTTPException(422, f"flight_date {given} doesn't match a departure {body.lead_time_hours:g} hours "
                                     f"from now ({flight_date})")
    cur = conn.execute(
        "INSERT INTO booking (awb, shipper_id, origin, destination, flight_date, commodity_text, commodity_class, "
        "pieces, weight_kg, volume_m3, declared_value_usd, payment_method, payment_ref, booked_at, lead_time_hours, "
        "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
        (number, body.shipper_id, body.origin.upper(), body.destination.upper(), flight_date.isoformat(),
         body.commodity_text, commodity.classify(body.commodity_text), body.pieces, body.weight_kg, body.volume_m3,
         body.declared_value_usd, body.payment_method, body.payment_ref, db.iso(db.utcnow()), body.lead_time_hours),
    )
    bid = int(cur.lastrowid)
    graph.add_links_for_booking(conn, body.shipper_id, body.payment_ref)
    conn.commit()
    res = risk.screen_booking(conn, bid, model)
    shared_payer = body.payment_ref and conn.execute(
        "SELECT COUNT(DISTINCT a_value) FROM contact_link WHERE a_type = 'shipper' AND b_type = 'payment_ref' "
        "AND b_value = ?", (body.payment_ref,)).fetchone()[0] > 1
    if res.band == "HIGH" or shared_payer:
        # A new HIGH booking flags the shipper; a payer account shared with someone
        # else is a new link in the graph. Either way the neighbours' scores move.
        risk.rescore_after_flag(conn, body.shipper_id, model, skip_booking=bid)
    return _booking_detail(conn, bid, request)


@app.post("/api/demo/simulate")
def api_simulate(request: Request, conn: sqlite3.Connection = Depends(get_conn),
                 model: anomaly.AnomalyModel = Depends(get_model)) -> dict[str, Any]:
    try:
        bid = simulate.simulate_booking(conn, model)
        r = conn.execute(_BOOKING_LIST_SQL + " WHERE b.id = ?", (bid,)).fetchone()
        if r is None:
            raise KeyError(bid)
    except (sqlite3.IntegrityError, KeyError):  # a reset landed mid-way; the next tick will work
        raise HTTPException(409, "The demo was reset while this booking was being created. Try again.")
    if r["band"] == "HIGH":
        risk.rescore_after_flag(conn, conn.execute("SELECT shipper_id FROM booking WHERE id = ?", (bid,)).fetchone()[0],
                                model, skip_booking=bid)
    return _list_item(r)


@app.get("/api/review-queue")
def api_review_queue(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        _BOOKING_LIST_SQL + " WHERE b.status IN ('in_review', 'on_hold')"
    ).fetchall()
    # Soonest departure first: that's the order a reviewer has to work in. Held items last.
    rows = sorted(rows, key=lambda r: (r["status"] == "on_hold",
                                       db.parse_dt(r["booked_at"]) + timedelta(hours=r["lead_time_hours"])))
    decided = conn.execute(
        """SELECT d.booking_id, d.decision, d.note, d.decided_at, b.awb, r.score
           FROM review_decision d JOIN booking b ON b.id = d.booking_id JOIN risk_score r ON r.booking_id = b.id
           ORDER BY d.decided_at DESC, d.id DESC LIMIT 20"""
    ).fetchall()
    items = []
    for r in rows:
        item = _list_item(r)
        item["reasons"] = json.loads(r["reasons_json"])
        items.append(item)
    return {"items": items,
            "recent_decisions": [{**dict(d), "awb": awbmod.fmt(d["awb"])} for d in decided]}


class ReviewBody(BaseModel):
    decision: Literal["approve", "hold", "reject"]
    note: str | None = Field(None, max_length=500)
    reviewer: str = Field("Demo reviewer", max_length=60)


@app.post("/api/review/{booking_id}")
def api_review(booking_id: int, body: ReviewBody, request: Request,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    b = conn.execute("SELECT status FROM booking WHERE id = ?", (booking_id,)).fetchone()
    if b is None:
        raise HTTPException(404, "No such booking")
    if b["status"] not in ("in_review", "on_hold"):
        raise HTTPException(409, f"Booking is {b['status']}, not waiting for review")
    if body.decision == "reject" and not (body.note or "").strip():
        raise HTTPException(422, "Add a note saying why before rejecting")
    status = {"approve": "accepted", "hold": "on_hold", "reject": "rejected"}[body.decision]
    conn.execute("INSERT INTO review_decision (booking_id, decision, note, decided_by, decided_at) VALUES (?, ?, ?, ?, ?)",
                 (booking_id, body.decision, (body.note or "").strip() or None, body.reviewer, db.iso(db.utcnow())))
    conn.execute("UPDATE booking SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    if body.decision == "reject":  # a refused shipment carries no cover
        conn.execute("UPDATE policy SET status = 'cancelled' WHERE booking_id = ? AND status = 'active'", (booking_id,))
        conn.commit()
    rescored = 0
    if body.decision in ("reject", "approve"):
        # The shipper's standing changed (flagged, or cleared): their other open
        # bookings and their contacts' bookings are rescored.
        shipper_id = conn.execute("SELECT shipper_id FROM booking WHERE id = ?", (booking_id,)).fetchone()[0]
        rescored = risk.rescore_after_flag(conn, shipper_id, get_model(request), skip_booking=booking_id)
    return {"booking_id": booking_id, "status": status, "rescored": rescored}


# ------------------------------------------------------------------ graph


@app.get("/graph/shipper/{shipper_id}")
def graph_shipper(shipper_id: int, hops: int = Query(2, ge=1, le=3),
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    try:
        return graph.subgraph(conn, shipper_id, hops)
    except KeyError:
        raise HTTPException(404, "No such shipper")


# ------------------------------------------------------------------ cover


@app.get("/api/quote/{booking_id}")
def api_quote(booking_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    try:
        return policies.quote_booking(conn, booking_id).to_dict()
    except KeyError:
        raise HTTPException(404, "No such booking")


class IssueBody(BaseModel):
    booking_id: int


@app.post("/api/policies", status_code=201)
def api_issue(body: IssueBody, request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    try:
        p = policies.issue(conn, body.booking_id)
    except KeyError:
        raise HTTPException(404, "No such booking")
    except policies.CoverError as e:
        raise HTTPException(409, str(e))
    return policies.to_dict(p, base_url(request))


@app.get("/api/policies/{pid}/qr.png")
def api_policy_qr(pid: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    p = policies.get(conn, pid)
    if p is None:
        raise HTTPException(404, "No such policy")
    return Response(policies.qr_png(policies.verify_url(base_url(request), p["qr_token"])), media_type="image/png")


@app.get("/api/policies/{pid}/certificate.png")
def api_policy_certificate(pid: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    try:
        png = policies.certificate_png(conn, pid, base_url(request))
    except KeyError:
        raise HTTPException(404, "No such policy")
    return Response(png, media_type="image/png",
                    headers={"Content-Disposition": f'inline; filename="{policies.policy_no(pid)}.png"'})


# ------------------------------------------------------------------ verify (public)


def _result_page(res: verify.VerifyResult) -> HTMLResponse:
    colours = {"GENUINE": ("#0f7b4f", "#e8f6ef"), "NOT FOUND": ("#b42318", "#fdecea"),
               "SUSPICIOUS": ("#b54708", "#fef4e6"), "INVALID FORMAT": ("#475467", "#f2f4f7"),
               "NOT ACTIVE": ("#b54708", "#fef4e6"), "UNREADABLE": ("#475467", "#f2f4f7")}
    fg, bg = colours.get(res.result, ("#475467", "#f2f4f7"))
    rows = ""
    labels = {"awb": "Air waybill", "origin": "From", "destination": "To", "flight_date": "Flight date",
              "pieces": "Pieces", "weight_kg": "Weight (kg)", "carriage_status": "Status", "policy_no": "Cover",
              "route": "Route", "sum_insured_usd": "Sum insured (USD)", "status": "Status", "issued_at": "Issued",
              "cover_ends": "Cover ends", "ended_on": "Cover ended"}
    for k, v in (res.details or {}).items():
        if k == "weight_kg":
            v = f"{v:,.1f}" if v < 100 else f"{v:,.0f}"
        elif isinstance(v, float):
            v = f"{v:,.2f}" if k.endswith("usd") else f"{v:,.0f}"
        elif k in ("flight_date", "issued_at", "cover_ends", "ended_on") and isinstance(v, str):
            v = verify.nice_date(v)
        elif k == "status" and isinstance(v, str):
            v = v.capitalize()
        rows += f"<tr><th>{html.escape(labels.get(k, k))}</th><td>{html.escape(str(v))}</td></tr>"
    airline = html.escape(config.airline_name())
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CargoShield verification</title>
<style>
body{{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#f5f6f8;color:#101828}}
main{{max-width:480px;margin:0 auto;padding:24px 16px}}
.brand{{font-weight:700;letter-spacing:.02em}} .sub{{color:#667085;font-size:13px}}
.card{{background:#fff;border-radius:14px;padding:20px;margin-top:16px;box-shadow:0 1px 3px rgba(16,24,40,.08)}}
.result{{display:inline-block;padding:6px 12px;border-radius:999px;font-weight:700;color:{fg};background:{bg}}}
table{{width:100%;border-collapse:collapse;margin-top:12px}} th{{text-align:left;color:#667085;font-weight:500;padding:6px 0;width:45%}}
td{{padding:6px 0}} footer{{color:#667085;font-size:12px;margin-top:20px}}
</style></head><body><main>
<div class="brand">CargoShield</div><div class="sub">Verification for {airline}</div>
<div class="card"><span class="result">{html.escape(res.result)}</span>
<p>{html.escape(res.message)}</p><table>{rows}</table></div>
<footer>{html.escape(_data_label())} Synthetic records only.</footer>
</main></body></html>"""
    return HTMLResponse(body)


@app.get("/verify/awb/{awb_text}")
def verify_awb(awb_text: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    html_view = wants_html(request)
    res = verify.verify_awb(conn, awb_text, channel="web" if html_view else "api", input_type="text")
    return _result_page(res) if html_view else res.to_dict()


@app.post("/verify/image")
async def verify_image(request: Request, file: UploadFile = File(...), conn: sqlite3.Connection = Depends(get_conn)):
    data = await file.read(ocr.MAX_UPLOAD_BYTES + 1)
    res = await run_in_threadpool(verify.verify_image, conn, data, "api", trusted_hosts(request))
    if res.result == verify.UNREADABLE:
        payload = res.to_dict()
        payload["ask"] = "Please type the air waybill number."
        return JSONResponse(payload, status_code=200)
    return res.to_dict()


@app.get("/verify/policy/{qr_token}")
def verify_policy(qr_token: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    html_view = wants_html(request)
    res = verify.verify_policy(conn, qr_token, channel="qr-scan" if html_view else "api", input_type="qr")
    return _result_page(res) if html_view else res.to_dict()


# ------------------------------------------------------------------ chat simulator


@app.post("/api/chat")
async def api_chat(request: Request, text: str | None = Form(None), image: UploadFile | None = File(None),
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    data = await image.read(ocr.MAX_UPLOAD_BYTES + 1) if image is not None else None
    msg = messaging.IncomingMessage(channel="chat-sim", sender="demo-user", text=text, image=data or None,
                                    image_mime=image.content_type if image is not None else None)
    # OCR and QR decoding block for up to a few seconds: keep them off the event loop.
    reply = await run_in_threadpool(messaging.handle_message, conn, msg, trusted_hosts(request))
    return reply.to_dict()


@app.get("/api/verifications")
def api_verifications(limit: int = Query(25, ge=1, le=200), conn: sqlite3.Connection = Depends(get_conn)):
    return [dict(r) for r in conn.execute(
        "SELECT id, channel, input_type, input_value, result, checked_at FROM verification_log "
        "ORDER BY checked_at DESC, id DESC LIMIT ?", (limit,))]


@app.get("/api/documents")
def api_documents(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    meta = db.get_meta(conn, "demo", {})
    fake_list = [{"name": f.filename, "url": f"/files/fakes/{f.filename}", "kind": f.kind,
                  "expected": f.expected, "story": f.story}
                 for f in fakes.FAKES if (config.fakes_dir() / f.filename).exists()]
    samples = [{"name": n, "url": f"/files/samples/{n}", "kind": "awb", "expected": "GENUINE",
                "story": "A genuine air waybill for a real (synthetic) booking."}
               for n in meta.get("samples", []) if (config.samples_dir() / n).exists()]
    return {"fakes": fake_list, "samples": samples}


@app.get("/files/{folder}/{name}", include_in_schema=False)
def files(folder: Literal["fakes", "samples"], name: str, conn: sqlite3.Connection = Depends(get_conn)):
    allowed = ({f.filename for f in fakes.FAKES} if folder == "fakes"
               else set(db.get_meta(conn, "demo", {}).get("samples", [])))
    if name not in allowed:
        raise HTTPException(404, "Not found")
    path = (config.fakes_dir() if folder == "fakes" else config.samples_dir()) / name
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


# ------------------------------------------------------------------ presenter tools


@app.post("/api/admin/reset")
def api_reset() -> dict[str, Any]:
    """Restore the demo start state (presenter's laptop only, see guard())."""
    try:
        db.restore()
    except FileNotFoundError:
        raise HTTPException(409, "No demo snapshot yet. Run `make seed` first.")
    return {"ok": True}
