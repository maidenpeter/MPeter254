"""Synthetic data generator (Milestone 1).

    python -m cargoshield.seed --bookings 3000

Builds a fresh database of shippers and bookings on the routes in
data/routes.csv, plants known risk patterns in about 4% of bookings, writes the
fake documents, then (unless --no-score) trains the anomaly model, scores every
booking, pre-loads some history and saves the demo start snapshot.

Everything is generated. Names are invented, emails use the reserved .example
domain, and phone numbers sit in the 88 block after the country code, which
Google's libphonenumber data lists as unassigned in every demo country (a test
checks it), so they should not reach a real phone.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import secrets
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from faker import Faker

from . import awb as awbmod
from . import commodity, config, db, fakes

DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}

# country -> (dial code, home airport, share of shippers, faker locale, city, primary class weights)
COUNTRIES: dict[str, tuple[str, str, float, str, str, dict[str, float]]] = {
    "KE": ("254", "NBO", 0.50, "en_KE", "Nairobi, Kenya",
           {"perishables": 0.38, "pharma": 0.12, "general_cargo": 0.16, "garments": 0.08,
            "documents": 0.08, "machinery_parts": 0.08, "electronics": 0.06, "valuables": 0.04}),
    "ZA": ("27", "JNB", 0.13, "zu_ZA", "Johannesburg, South Africa",
           {"machinery_parts": 0.35, "pharma": 0.2, "general_cargo": 0.2, "electronics": 0.1,
            "documents": 0.1, "valuables": 0.05}),
    "HK": ("852", "HKG", 0.12, "en_KE", "Hong Kong",
           {"electronics": 0.4, "mobile_phones": 0.3, "garments": 0.2, "general_cargo": 0.1}),
    "UG": ("256", "EBB", 0.05, "en_KE", "Kampala, Uganda",
           {"perishables": 0.5, "general_cargo": 0.3, "valuables": 0.1, "documents": 0.1}),
    "TZ": ("255", "DAR", 0.05, "sw", "Dar es Salaam, Tanzania",
           {"perishables": 0.4, "general_cargo": 0.3, "valuables": 0.1, "machinery_parts": 0.2}),
    "NG": ("234", "LOS", 0.04, "en_NG", "Lagos, Nigeria",
           {"general_cargo": 0.4, "garments": 0.2, "electronics": 0.2, "documents": 0.2}),
    "GH": ("233", "ACC", 0.02, "en_NG", "Accra, Ghana",
           {"general_cargo": 0.5, "perishables": 0.3, "documents": 0.2}),
    "RW": ("250", "KGL", 0.03, "en_KE", "Kigali, Rwanda",
           {"perishables": 0.5, "general_cargo": 0.3, "valuables": 0.2}),
    "ET": ("251", "ADD", 0.02, "en_KE", "Addis Ababa, Ethiopia",
           {"perishables": 0.6, "general_cargo": 0.4}),
    "ZM": ("260", "LUN", 0.02, "en_KE", "Lusaka, Zambia",
           {"machinery_parts": 0.5, "general_cargo": 0.3, "valuables": 0.2}),
    "ZW": ("263", "HRE", 0.01, "en_KE", "Harare, Zimbabwe",
           {"general_cargo": 0.6, "machinery_parts": 0.4}),
    "CD": ("243", "FIH", 0.01, "en_KE", "Kinshasa, DR Congo",
           {"general_cargo": 0.5, "valuables": 0.5}),
}

NAME_A = ["Savanna", "Baobab", "Jacaranda", "Acacia", "Sunbird", "Kestrel", "Equator", "Highland",
          "Lakeside", "Coastline", "Northgate", "Bluecrest", "Mtoni", "Kijani", "Nyota", "Mawimbi",
          "Pwani", "Tumaini", "Shwari", "Mvuli", "Anga", "Upepo", "Kasuku", "Baraka", "Tulivu", "Kibo",
          "Pembe", "Chui", "Mawingu", "Amani", "Kilele", "Rafiki", "Sokoni", "Ziwa", "Milele", "Tembo",
          "Juniper", "Silverleaf", "Redstone", "Greenfield", "Southwind", "Eastline", "Granite", "Hillcrest"]
NAME_B = {
    "perishables": ["Fresh", "Farms", "Agro Exports", "Horticulture", "Growers", "Produce"],
    "electronics": ["Electronics", "Tech Supplies", "Digital", "Systems"],
    "mobile_phones": ["Mobile", "Telecom Devices", "Devices", "Handsets"],
    "pharma": ["Pharma", "Medical Supplies", "Health Supplies", "Lifesciences"],
    "machinery_parts": ["Engineering", "Machinery", "Industrial Supplies", "Equipment"],
    "general_cargo": ["Trading", "Supplies", "General Merchants", "Imports"],
    "documents": ["Consulting", "Advisory", "Couriers", "Legal Services"],
    "garments": ["Textiles", "Apparel", "Garments", "Fashion House"],
    "valuables": ["Minerals", "Refiners", "Metals", "Bullion Trading"],
}
NAME_C = {"ZA": ["(Pty) Ltd"], "HK": ["Co. Ltd", "Limited", "Trading Co."], "CD": ["SARL"],
          "ET": ["PLC"], "default": ["Ltd", "Limited", "Ltd"]}
EMAIL_LOCAL = ["orders", "info", "shipping", "exports", "logistics", "accounts", "ops", "cargo"]

# Fixed serials for the demo's hero bookings, so DEMO.md can name them.
HERO_HIGH_SERIAL = "6042218"
HERO_CLEAN_SERIAL = "3150946"

PLANTED_PATTERNS = (
    "new_shipper_high_value",
    "vague_description",
    "weight_volume_mismatch",
    "late_booking_risky_payment",
    "shared_contact_flagged",
)


# Time zone of each airport, so the local departure times in routes.csv become
# real moments. Add a row here (or a "timezone" column in routes.csv) for any
# airport a refreshed schedule brings in; unknown airports fall back to UTC.
AIRPORT_TZ = {
    "NBO": "Africa/Nairobi", "JNB": "Africa/Johannesburg", "HKG": "Asia/Hong_Kong",
    "LOS": "Africa/Lagos", "ACC": "Africa/Accra", "EBB": "Africa/Kampala",
    "DAR": "Africa/Dar_es_Salaam", "KGL": "Africa/Kigali", "FIH": "Africa/Kinshasa",
    "LUN": "Africa/Lusaka", "HRE": "Africa/Harare", "ADD": "Africa/Addis_Ababa",
    "DXB": "Asia/Dubai", "DOH": "Asia/Qatar", "AMS": "Europe/Amsterdam", "LHR": "Europe/London",
    "LGG": "Europe/Brussels", "CDG": "Europe/Paris", "MBA": "Africa/Nairobi", "KIS": "Africa/Nairobi",
    "MRU": "Indian/Mauritius", "CPT": "Africa/Johannesburg", "BOM": "Asia/Kolkata", "CAN": "Asia/Shanghai",
    "PVG": "Asia/Shanghai", "BKK": "Asia/Bangkok", "SIN": "Asia/Singapore", "JED": "Asia/Riyadh",
}


def _zone(name: str):
    """The airport's time zone, or plain UTC if it (or the tz database) is missing."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def origin_zone(airport: str):
    """Time zone for an origin airport: routes.csv's timezone column, then the table above."""
    for r in load_routes():
        if r.origin == airport:
            return _zone(r.tz)
    return _zone(AIRPORT_TZ.get(airport, "UTC"))


@dataclass(frozen=True)
class Route:
    """A scheduled route. `departure` is the local time at the origin airport.

    All datetimes going in and out are naive UTC, like everything else stored.
    """

    origin: str
    destination: str
    days: frozenset  # local weekdays, Monday = 0
    departure: time
    tz: str = "UTC"

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}"

    def _utc(self, local_day: date) -> datetime:
        local = datetime.combine(local_day, self.departure, tzinfo=_zone(self.tz))
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def local_date(self, dep_utc: datetime) -> date:
        """The flight date as printed on the AWB: the local date at the origin."""
        return dep_utc.replace(tzinfo=timezone.utc).astimezone(_zone(self.tz)).date()

    def flies_on(self, dep_utc: datetime) -> bool:
        return self.local_date(dep_utc).weekday() in self.days

    def next_departure(self, after: datetime) -> datetime:
        start = self.local_date(after)
        for i in range(-1, 9):
            day = start + timedelta(days=i)
            if day.weekday() in self.days:
                dt = self._utc(day)
                if dt >= after:
                    return dt
        raise RuntimeError(f"route {self.key} has no departures")

    def prev_departure(self, before: datetime) -> datetime:
        start = self.local_date(before)
        for i in range(-1, 9):
            day = start - timedelta(days=i)
            if day.weekday() in self.days:
                dt = self._utc(day)
                if dt <= before:
                    return dt
        raise RuntimeError(f"route {self.key} has no departures")


# sha256 of the routes.csv this repo ships with. `make doctor` uses it to remind
# the presenter that the schedule is still the placeholder one.
PLACEHOLDER_ROUTES_SHA256 = "6eb2e1b94d3fd94c07be321f49ab0f2e0f6d792d78ddce0e2775b8fb19668455"


def routes_are_placeholder() -> bool:
    import hashlib

    try:
        data = config.routes_csv().read_bytes().replace(b"\r\n", b"\n")
    except FileNotFoundError:
        return False
    return hashlib.sha256(data).hexdigest() == PLACEHOLDER_ROUTES_SHA256


def load_routes(path: str | Path | None = None) -> list[Route]:
    routes = []
    with open(path or config.routes_csv(), newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw_days = row["days_of_week"].strip()
            if raw_days.lower() == "daily":
                days = frozenset(range(7))
            else:
                days = frozenset(DAYS[d.strip()[:3].title()] for d in raw_days.split("|") if d.strip())
            hh, mm = (row.get("departure_local") or "12:00").split(":")
            origin = row["origin"].strip().upper()
            tz = (row.get("timezone") or "").strip() or AIRPORT_TZ.get(origin, "UTC")
            routes.append(Route(origin, row["destination"].strip().upper(), days, time(int(hh), int(mm)), tz))
    if not routes:
        raise ValueError("routes.csv has no routes")
    return routes


TROY_OZ_PER_KG = 32.1507


def gold_usd_per_kg() -> float:
    return float(config.commodities()["reference_prices"]["gold_usd_per_oz"]) * TROY_OZ_PER_KG


class Generator:
    def __init__(self, seed: int, anchor: datetime, history_days: int):
        self.rng = random.Random(seed)
        self.seed = seed
        self.anchor = anchor.replace(second=0, microsecond=0)
        self.start = self.anchor - timedelta(days=history_days)
        self.prefix = config.awb_prefix()
        self.routes = load_routes()
        self.routes_by_origin: dict[str, list[Route]] = defaultdict(list)
        for r in self.routes:
            self.routes_by_origin[r.origin].append(r)
        self.used_serials = set(fakes.RESERVED_SERIALS) | {HERO_HIGH_SERIAL, HERO_CLEAN_SERIAL}
        self.used_names: set[str] = set()
        self.used_phones: set[str] = set()
        self.used_emails: set[str] = set()
        self.used_refs: set[str] = set()
        self.fakers: dict[str, Faker] = {}
        self.cls_cfg = commodity.classes()

    # ---------- small helpers ----------
    def faker(self, locale: str) -> Faker:
        if locale not in self.fakers:
            f = Faker(locale)
            f.seed_instance(self.seed + len(self.fakers))
            self.fakers[locale] = f
        return self.fakers[locale]

    def pick(self, weights: dict[str, float]) -> str:
        keys = list(weights)
        return self.rng.choices(keys, weights=[weights[k] for k in keys])[0]

    def serial(self) -> str:
        while True:
            s = str(self.rng.randint(1_000_000, 9_999_999))
            if s not in self.used_serials:
                self.used_serials.add(s)
                return s

    def awb(self, serial7: str | None = None) -> str:
        return awbmod.make(self.prefix, serial7 or self.serial())

    def company_name(self, country: str, cls: str) -> str:
        suffixes = NAME_C.get(country, NAME_C["default"])
        for attempt in range(60):
            a = self.rng.choice(NAME_A)
            b = self.rng.choice(NAME_B[cls])
            name = f"{a} {b} {self.rng.choice(suffixes)}"
            if attempt > 30:
                name = f"{a} {self.rng.choice(NAME_A)} {b} {self.rng.choice(suffixes)}"
            if name not in self.used_names:
                self.used_names.add(name)
                return name
        raise RuntimeError("ran out of company names")

    def phone(self, country: str) -> str:
        # Every number starts 88 after the country code: a block Google's
        # libphonenumber data lists as unassigned in every demo country, so the
        # numbers look normal but should not reach a real phone. A test checks
        # this whenever the `phonenumbers` package is installed.
        dial = COUNTRIES[country][0]
        n = 8 if country == "HK" else 9
        while True:
            nsn = "88" + "".join(str(self.rng.randint(0, 9)) for _ in range(n - 2))
            p = f"+{dial} {nsn[:4]} {nsn[4:]}" if n == 8 else f"+{dial} {nsn[:3]} {nsn[3:6]} {nsn[6:]}"
            if p not in self.used_phones:
                self.used_phones.add(p)
                return p

    def email(self, name: str, country: str, personal: bool = False) -> str:
        f = self.faker(COUNTRIES[country][3])
        while True:
            if personal:
                local = f"{f.first_name().lower()}.{f.last_name().lower()}{self.rng.randint(1, 99)}"
                domain = self.rng.choice(["mail.example", "inbox.example", "post.example"])
            else:
                words = [w for w in name.lower().replace("(pty)", "").replace(".", "").split()
                         if w not in {"ltd", "limited", "co", "sarl", "plc", "&"}]
                domain = "-".join(words[:2]) + ".example"
                local = self.rng.choice(EMAIL_LOCAL)
                if self.rng.random() < 0.3:
                    local = f"{f.first_name().lower()}.{f.last_name().lower()}"
            local = "".join(ch for ch in local if ch.isalnum() or ch in ".-_")
            e = f"{local}@{domain}"
            if e not in self.used_emails:
                self.used_emails.add(e)
                return e

    def payer_ref(self, kind: str = "ACC") -> str:
        while True:
            r = f"{kind}-{self.rng.randint(10_000_000, 99_999_999)}"
            if r not in self.used_refs:
                self.used_refs.add(r)
                return r

    def uniform_dt(self, lo: datetime, hi: datetime) -> datetime:
        if hi <= lo:
            return lo
        return lo + timedelta(seconds=self.rng.uniform(0, (hi - lo).total_seconds()))

    def route_from(self, airport: str, prefer_dest: list[str] | None = None) -> Route:
        options = self.routes_by_origin.get(airport) or self.routes_by_origin.get("NBO") or self.routes
        if prefer_dest:
            preferred = [r for r in options if r.destination in prefer_dest]
            if preferred and self.rng.random() < 0.8:
                return self.rng.choice(preferred)
        return self.rng.choice(options)

    def valuables_vpk(self, text: str) -> float:
        """Declared value per kg for valuables, by description (gold follows the spot price)."""
        spec = self.cls_cfg["valuables"].get("priced_by_description", {}).get(text)
        if not spec:
            lo, hi = self.cls_cfg["valuables"]["value_per_kg"]
            return self.log_uniform(lo, hi, skew=0.5)
        if "gold_spot_share" in spec:
            lo, hi = spec["gold_spot_share"]
            return gold_usd_per_kg() * self.rng.uniform(lo, hi)
        lo, hi = spec["usd_per_kg"]
        return self.log_uniform(lo, hi, skew=0.5)

    def log_uniform(self, lo: float, hi: float, skew: float = 0.35) -> float:
        a, b = math.log(lo), math.log(hi)
        return math.exp(self.rng.triangular(a, b, a + skew * (b - a)))

    # ---------- shippers ----------
    def make_shipper(self, country: str, cls: str, created_at: datetime, kyc: str,
                     planted: bool = False, personal_email: bool = False) -> dict[str, Any]:
        name = self.company_name(country, cls)
        return {
            "name": name,
            "country": country,
            "phone": self.phone(country),
            "email": self.email(name, country, personal=personal_email),
            "created_at": db.iso(created_at),
            "kyc_status": kyc,
            "is_planted_fraud": 1 if planted else 0,
            # generator-only fields (not stored)
            "_cls": cls,
            "_hub": COUNTRIES[country][1],
            "_payer": self.payer_ref("ACC"),
            "_credit": None,
            "_new": False,
        }

    def clean_shipper(self) -> dict[str, Any]:
        country = self.pick({c: v[2] for c, v in COUNTRIES.items()})
        cls = self.pick(COUNTRIES[country][5])
        if self.rng.random() < 0.82:
            created = self.anchor - timedelta(days=self.rng.uniform(365, 12 * 365))
            s = self.make_shipper(country, cls, created, "verified")
        else:
            created = self.anchor - timedelta(days=self.rng.uniform(4, 150))
            age = (self.anchor - created).days
            kyc = "pending" if (age < 60 and self.rng.random() < 0.6) else "verified"
            s = self.make_shipper(country, cls, created, kyc)
            s["_new"] = True
        s["_credit"] = f"CRA-{self.rng.randint(100000, 999999)}" if not s["_new"] else None
        return s

    # ---------- bookings ----------
    def _physical(self, cls: str, weight: float | None = None, density: float | None = None,
                  vpk: float | None = None) -> dict[str, Any]:
        spec = self.cls_cfg[cls]
        wlo, whi = spec["weight_kg"]
        dlo, dhi = spec["density_kg_m3"]
        vlo, vhi = spec["value_per_kg"]
        if weight is None:
            weight = self.log_uniform(wlo, whi)
        if density is None:
            density = min(max(spec["typical_density"] * math.exp(self.rng.gauss(0, 0.18)), dlo), dhi)
        if vpk is None:
            vpk = self.log_uniform(vlo, vhi, skew=0.5)
        weight = round(weight, 1) if weight < 100 else round(weight)
        volume = max(round(weight / density, 4), 0.0003)
        pieces = max(1, round(weight / spec["piece_kg"] * self.rng.uniform(0.7, 1.3)))
        return {"commodity_class": cls, "weight_kg": float(weight), "volume_m3": float(volume),
                "pieces": int(pieces), "declared_value_usd": float(round(weight * vpk, -1) or 10.0)}

    def _timing(self, route: Route, shipper: dict, lead_target: float | None = None,
                late: tuple[float, float] | None = None) -> tuple[datetime, datetime]:
        """Return (booked_at, departure). Bookings are never in the future."""
        earliest = max(self.start, db.parse_dt(shipper["created_at"]) + timedelta(hours=6))
        latest = self.anchor - timedelta(minutes=30)
        if late:
            for _ in range(80):
                lead = self.rng.uniform(*late)
                dep = route.next_departure(self.uniform_dt(earliest + timedelta(hours=lead), latest + timedelta(hours=lead)))
                booked = dep - timedelta(hours=lead)
                if earliest <= booked <= latest:
                    return booked, dep
            dep = route.prev_departure(latest + timedelta(hours=late[0]))
            return dep - timedelta(hours=late[0]), dep
        for _ in range(80):
            booked = self.uniform_dt(earliest, latest)
            lt = lead_target if lead_target is not None else min(max(self.rng.lognormvariate(math.log(60), 0.75), 4), 400)
            dep = route.next_departure(booked + timedelta(hours=lt))
            return booked, dep
        raise RuntimeError("timing")

    def booking(self, shipper: dict, cls: str, text: str, route: Route, phys: dict,
                payment: str, booked: datetime, dep: datetime, serial7: str | None = None,
                payment_ref: str | None = None) -> dict[str, Any]:
        if payment_ref is None:
            payment_ref = {
                "credit_account": shipper.get("_credit") or shipper["_payer"],
                "bank_transfer": shipper["_payer"],
                "card": shipper["_payer"],
                "mobile_money": shipper["_payer"],
                "third_party": self.payer_ref("TPP"),
                "cash": None,
            }[payment]
        return {
            "awb": self.awb(serial7),
            "_shipper": shipper,
            "origin": route.origin,
            "destination": route.destination,
            "flight_date": route.local_date(dep).isoformat(),
            "commodity_text": text,
            **phys,
            "payment_method": payment,
            "payment_ref": payment_ref,
            # Round the lead time, then move the booking time to match, so booked_at +
            # lead_time is exactly the scheduled departure.
            "booked_at": db.iso(dep - timedelta(hours=round((dep - booked).total_seconds() / 3600.0, 1))),
            "lead_time_hours": round((dep - booked).total_seconds() / 3600.0, 1),
            "status": "new",
            "_patterns": [],
        }

    def clean_payment(self, shipper: dict) -> str:
        if not shipper["_new"]:
            return self.pick({"credit_account": 0.6, "bank_transfer": 0.3, "card": 0.04, "mobile_money": 0.06})
        return self.pick({"card": 0.3, "mobile_money": 0.3, "bank_transfer": 0.3, "cash": 0.07, "third_party": 0.03})

    def clean_booking(self, shipper: dict) -> dict[str, Any]:
        cls = shipper["_cls"]
        if self.rng.random() < 0.15:
            cls = self.rng.choice([c for c in self.cls_cfg if c != "valuables"])
        text = self.rng.choice(self.cls_cfg[cls]["descriptions"])
        phys = None
        if cls == "general_cargo" and self.rng.random() < 0.08:
            # Ordinary small shipments with vague wording. Harmless noise.
            text = self.rng.choice(["Personal effects", "Samples", "Assorted items"])
            phys = self._physical(cls, weight=self.rng.uniform(5, 60), vpk=self.rng.uniform(4, 25))
        if phys is None and cls == "valuables":
            phys = self._physical(cls, vpk=self.valuables_vpk(text))
        phys = phys or self._physical(cls)
        route = self.route_from(shipper["_hub"])
        lead = None
        if cls == "perishables":
            lead = min(max(self.rng.lognormvariate(math.log(30), 0.7), 4), 200)
        booked, dep = self._timing(route, shipper, lead_target=lead)
        return self.booking(shipper, cls, text, route, phys, self.clean_payment(shipper), booked, dep)


def pick_hero_flight(g: Generator, home: str = "NBO") -> tuple[Route, datetime, datetime]:
    """Flight for the demo's HIGH booking: (route, departure, booked_at).

    The story is a new Nairobi shipper sending gold, "caught before the plane
    left". So the flight leaves from `home`, is still in the future, was booked a
    few hours out (under the 12-hour rule), and was never booked in the future.
    Prefer the next scheduled departure from home within 11 hours, ideally to
    Hong Kong or Johannesburg. If home has nothing that soon (e.g. a quiet
    Thursday), use a home route that flies today and set the departure 6 hours
    out; only the departure time is then off the timetable (the drawer shows
    it). Other airports are a last resort for a refreshed schedule without home.
    """
    soonest = g.anchor + timedelta(hours=4)  # leaves time to present after `make demo`
    latest = g.anchor + timedelta(hours=11)
    for pool in ([r for r in g.routes if r.origin == home], g.routes):
        candidates = [(r.next_departure(soonest), r) for r in pool]
        candidates = [(d, r) for d, r in candidates if d <= latest]
        if candidates:
            def preference(c):
                d, r = c
                return (r.destination not in ("HKG", "JNB"), abs((d - g.anchor).total_seconds() / 3600 - 5))
            dep, route = min(candidates, key=preference)
            booked = min(dep - timedelta(hours=7), g.anchor - timedelta(minutes=30))
            return route, dep, booked
        dep = g.anchor + timedelta(hours=6)
        flying = [r for r in pool if r.flies_on(dep)]
        if flying:
            route = min(flying, key=lambda r: r.destination not in ("HKG", "JNB"))
            return route, dep, dep - timedelta(hours=7)
    route = g.routes[0]  # degenerate schedule: take the next departure whatever it is
    dep = route.next_departure(soonest)
    return route, dep, g.anchor - timedelta(minutes=30)


def _insert(conn, table: str, row: dict[str, Any]) -> int:
    cols = [k for k in row if not k.startswith("_")]
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    return int(cur.lastrowid)


def generate(conn, n_bookings: int, n_shippers: int, seed: int, planted_fraction: float,
             history_days: int, anchor: datetime) -> dict[str, Any]:
    """Generate shippers and bookings into an empty database."""
    g = Generator(seed, anchor, history_days)
    rng = g.rng
    shippers: list[dict] = []
    bookings: list[dict] = []

    # The ring (12 bookings) plus at least two of each other pattern means seeds
    # under about 1,000 bookings plant a little more than 5%.
    n_planted = max(20, round(n_bookings * planted_fraction))

    # ---------------- planted fraud ring (pattern 5) ----------------
    hero_route, hero_dep, hero_booked = pick_hero_flight(g)

    r1 = g.make_shipper("KE", "valuables", hero_booked - timedelta(days=12, hours=5), "pending", planted=True, personal_email=True)
    r2 = g.make_shipper("KE", "general_cargo", g.anchor - timedelta(days=730), "verified", planted=True)
    r3 = g.make_shipper("UG", "garments", g.anchor - timedelta(days=200), "failed", planted=True, personal_email=True)
    r4 = g.make_shipper("HK", "mobile_phones", g.anchor - timedelta(days=70), "verified", planted=True)
    r5 = g.make_shipper("KE", "general_cargo", g.anchor - timedelta(days=400), "pending", planted=True, personal_email=True)
    ring = [r1, r2, r3, r4, r5]
    ring_payer = g.payer_ref("ACC")
    r2["phone"] = r1["phone"]
    r3["phone"] = r1["phone"]
    r4["email"] = r3["email"]
    for r in (r1, r4, r5):
        r["_payer"] = ring_payer
    shippers.extend(ring)

    # Hero HIGH booking: new shipper, gold, booked 7 hours out, cash.
    hero_value = float(round(6.0 * gold_usd_per_kg(), -3))  # 6 kg at the configured gold spot price
    hero = g.booking(r1, "valuables", "Gold bars", hero_route,
                     {"commodity_class": "valuables", "weight_kg": 6.0, "volume_m3": 0.002, "pieces": 2,
                      "declared_value_usd": hero_value},
                     "cash", hero_booked, hero_dep, serial7=HERO_HIGH_SERIAL)
    hero["_patterns"] = ["new_shipper_high_value", "late_booking_risky_payment", "shared_contact_flagged"]
    bookings.append(hero)
    # R1's second booking: gemstones, paid by the shared payer account
    rt = g.route_from("NBO", ["JNB", "HKG"])
    b = hero_booked - timedelta(days=9, hours=3)  # flew last week; the seeded history refuses it
    d = rt.next_departure(b + timedelta(hours=30))
    x = g.booking(r1, "valuables", "Gemstones", rt, g._physical("valuables", weight=2.0, vpk=45000),
                  "third_party", b, d, payment_ref=ring_payer)
    x["_patterns"] = ["new_shipper_high_value", "shared_contact_flagged"]
    bookings.append(x)

    def ring_booking(s: dict, cls: str, text: str, payment: str, ref: str | None = None,
                     weight: float | None = None, vpk: float | None = None) -> None:
        rt = g.route_from(s["_hub"])
        b, d = g._timing(rt, s)
        bk = g.booking(s, cls, text, rt, g._physical(cls, weight=weight, vpk=vpk), payment, b, d, payment_ref=ref)
        bk["_patterns"] = ["shared_contact_flagged"]
        bookings.append(bk)

    for _ in range(3):
        ring_booking(r2, "general_cargo", rng.choice(g.cls_cfg["general_cargo"]["descriptions"]), "bank_transfer")
    for _ in range(2):
        ring_booking(r3, "garments", rng.choice(g.cls_cfg["garments"]["descriptions"]), "mobile_money")
    for pay in ("third_party", "mobile_money", "third_party"):
        ring_booking(r4, "mobile_phones", rng.choice(g.cls_cfg["mobile_phones"]["descriptions"]), pay, ref=ring_payer)
    for _ in range(2):
        ring_booking(r5, "general_cargo", "Personal effects", "mobile_money", ref=ring_payer,
                     weight=rng.uniform(25, 60), vpk=rng.uniform(5, 20))

    # ---------------- other planted patterns ----------------
    remaining = max(n_planted - len(bookings), 8)
    quotas = {
        "new_shipper_high_value": round(remaining * 0.28),
        "vague_description": round(remaining * 0.24),
        "weight_volume_mismatch": round(remaining * 0.24),
    }
    quotas["late_booking_risky_payment"] = remaining - sum(quotas.values())

    def planted_shipper(pattern: str) -> dict:
        if pattern == "new_shipper_high_value":
            cls = rng.choice(["valuables", "valuables", "mobile_phones"])
            country = rng.choice(["KE", "UG", "TZ", "CD"]) if cls == "valuables" else "HK"
            created = g.anchor - timedelta(days=rng.uniform(3, 40))
            s = g.make_shipper(country, cls, created, rng.choice(["pending", "pending", "verified"]),
                               planted=True, personal_email=rng.random() < 0.7)
        else:
            country = g.pick({c: v[2] for c, v in COUNTRIES.items()})
            cls = "general_cargo"
            if rng.random() < 0.5:
                created = g.anchor - timedelta(days=rng.uniform(60, 2000))
                kyc = "verified"
            else:
                created = g.anchor - timedelta(days=rng.uniform(40, 200))
                kyc = rng.choice(["verified", "pending"])
            s = g.make_shipper(country, cls, created, kyc, planted=True, personal_email=rng.random() < 0.5)
        s["_new"] = True
        shippers.append(s)
        return s

    for pattern, quota in quotas.items():
        made = 0
        while made < quota:
            s = planted_shipper(pattern)
            for _ in range(min(rng.randint(2, 4), quota - made)):
                bk = None
                if pattern == "new_shipper_high_value":
                    created = db.parse_dt(s["created_at"])
                    rt = g.route_from(s["_hub"], ["HKG", "JNB"] if s["_cls"] == "valuables" else ["NBO", "JNB"])
                    cls = s["_cls"]
                    choices = list(g.cls_cfg[cls]["descriptions"]) + list(g.cls_cfg[cls].get("descriptions_planted_extra", []))
                    text = rng.choice(choices)
                    phys = (g._physical(cls, weight=rng.uniform(2, 28), vpk=g.valuables_vpk(text))
                            if cls == "valuables" else
                            g._physical(cls, weight=rng.uniform(60, 500), vpk=rng.uniform(300, 1200)))
                    window_end = min(created + timedelta(days=27), g.anchor - timedelta(minutes=30))
                    also_late = rng.random() < 0.4
                    for _try in range(60):
                        if also_late:
                            booked, dep = g._timing(rt, s, late=(3, 11))
                        else:
                            booked, dep = g._timing(rt, s, lead_target=rng.uniform(20, 120))
                        if booked <= window_end:
                            break
                    else:
                        # Pull the booking inside the shipper's first four weeks.
                        booked = g.uniform_dt(created + timedelta(hours=8), window_end)
                        dep = rt.next_departure(booked + timedelta(hours=24))
                    pay = rng.choice(["cash", "third_party"]) if also_late else rng.choice(["card", "mobile_money", "bank_transfer"])
                    bk = g.booking(s, cls, text, rt, phys, pay, booked, dep)
                    bk["_patterns"] = ["new_shipper_high_value"] + (["late_booking_risky_payment"] if also_late else [])
                elif pattern == "vague_description":
                    rt = g.route_from(s["_hub"])
                    text = rng.choice(commodity_cfg_vague())
                    if rng.random() < 0.5:
                        phys = g._physical("general_cargo", weight=rng.uniform(150, 900),
                                           density=rng.uniform(700, 1600), vpk=rng.uniform(5, 30))
                    else:
                        w = rng.uniform(40, 300)
                        phys = g._physical("general_cargo", weight=w, vpk=rng.uniform(25000, 180000) / w)
                    booked, dep = g._timing(rt, s)
                    bk = g.booking(s, "general_cargo", text, rt, phys, rng.choice(["bank_transfer", "mobile_money", "card"]), booked, dep)
                    bk["_patterns"] = ["vague_description"]
                elif pattern == "weight_volume_mismatch":
                    rt = g.route_from(s["_hub"])
                    if rng.random() < 0.5:
                        cls, text = "documents", rng.choice(["Documents", "Business documents", "Courier documents"])
                        phys = g._physical(cls, weight=rng.uniform(120, 600), vpk=rng.uniform(1, 5))
                    else:
                        cls = rng.choice(["perishables", "garments"])
                        text = rng.choice(g.cls_cfg[cls]["descriptions"])
                        phys = g._physical(cls, weight=rng.uniform(200, 1500), density=rng.uniform(1000, 2200))
                    booked, dep = g._timing(rt, s)
                    bk = g.booking(s, cls, text, rt, phys, rng.choice(["bank_transfer", "mobile_money", "card"]), booked, dep)
                    bk["_patterns"] = ["weight_volume_mismatch"]
                else:  # late_booking_risky_payment
                    rt = g.route_from(s["_hub"])
                    cls = rng.choice(["general_cargo", "electronics", "garments", "machinery_parts"])
                    text = rng.choice(g.cls_cfg[cls]["descriptions"])
                    booked, dep = g._timing(rt, s, late=(2, 11))
                    pay = "cash" if rng.random() < 0.6 else "third_party"
                    bk = g.booking(s, cls, text, rt, g._physical(cls), pay, booked, dep)
                    bk["_patterns"] = ["late_booking_risky_payment"]
                bookings.append(bk)
                made += 1

    # ---------------- clean shippers and bookings ----------------
    n_clean_shippers = max(n_shippers - len(shippers), 20)
    clean_shippers = [g.clean_shipper() for _ in range(n_clean_shippers)]

    # Clean hero: an established pharma exporter in Nairobi.
    hero_shipper = g.make_shipper("KE", "pharma", g.anchor - timedelta(days=8 * 365 + 40), "verified")
    hero_shipper["_credit"] = f"CRA-{rng.randint(100000, 999999)}"
    clean_shippers[0] = hero_shipper

    # Freight forwarders: a few clean shippers book through the same agent phone.
    # Shared contacts on their own are not a reason to flag anyone.
    ke_clean = [s for s in clean_shippers[1:] if s["country"] == "KE"]
    rng.shuffle(ke_clean)
    for i in range(4):
        group = ke_clean[i * 5:(i + 1) * 5]
        if len(group) < 2:
            break
        agent_phone = g.phone("KE")
        for s in group:
            s["phone"] = agent_phone
    shippers.extend(clean_shippers)

    activity = [rng.lognormvariate(0, 1.0) * (0.35 if s["_new"] else 1.0) for s in clean_shippers]
    n_clean = max(n_bookings - len(bookings), 0)

    lun = (next((r for r in g.routes if r.key == "NBO-LUN"), None)
           or next((r for r in g.routes if r.origin == "NBO"), None) or g.routes[0])
    dep = lun.next_departure(g.anchor + timedelta(days=2))
    clean_hero = g.booking(hero_shipper, "pharma", "Pharmaceuticals", lun,
                           {"commodity_class": "pharma", "weight_kg": 420.0, "volume_m3": 2.1, "pieces": 21,
                            "declared_value_usd": 68500.0},
                           "credit_account", g.anchor - timedelta(hours=2), dep, serial7=HERO_CLEAN_SERIAL)
    bookings.append(clean_hero)
    n_clean -= 1

    for s in rng.choices(clean_shippers, weights=activity, k=max(n_clean, 0)):
        bookings.append(g.clean_booking(s))

    # ---------------- write ----------------
    for s in shippers:
        s["id"] = _insert(conn, "shipper", s)
    bookings.sort(key=lambda b: b["booked_at"])
    for b in bookings:
        b["shipper_id"] = b["_shipper"]["id"]
        b["id"] = _insert(conn, "booking", b)
        for p in b["_patterns"]:
            conn.execute("INSERT INTO seed_truth (booking_id, pattern) VALUES (?, ?)", (b["id"], p))
    conn.commit()

    return {
        "anchor": db.iso(g.anchor),
        "shippers": len(shippers),
        "bookings": len(bookings),
        "planted_bookings": sum(1 for b in bookings if b["_patterns"]),
        "ring_shipper_ids": [s["id"] for s in ring],
        "hero_high_booking_id": hero["id"],
        "hero_high_awb": hero["awb"],
        "hero_clean_booking_id": clean_hero["id"],
        "hero_clean_awb": clean_hero["awb"],
    }


def commodity_cfg_vague() -> list[str]:
    return config.commodities()["vague"]["descriptions"]


# ---------------------------------------------------------------- history

SEED_REVIEWER = "Cargo security desk"
REJECT_NOTES = {
    "shipper_previously_rejected": ["Same shipper as an earlier refused booking. Not accepted.",
                                    "Repeat of a shipment refused last week. Not accepted."],
    "linked_to_flagged_shipper": ["Shares contact details with a shipper already under review. Not accepted.",
                                  "Phone number matches a refused shipper. Booking cancelled."],
    "watchlist_shipper": ["Shipper is on the internal watchlist. Not accepted."],
    "new_shipper_high_value": ["New account could not show proof of ownership for the goods. Not accepted.",
                               "No invoice or export permit for the goods. Refused.",
                               "Account opened {age} before booking, no trading history. Refused."],
    "late_booking_risky_payment": ["Payer could not be identified before cut-off. Booking cancelled.",
                                   "Cash payer would not give ID. Not accepted."],
    "vague_description_dense": ["Contents did not match the declaration at acceptance. Refused.",
                                "Shipper would not open the boxes for inspection. Refused."],
    "vague_description_high_value": ["Shipper could not describe the goods or show an invoice. Not accepted."],
    "weight_volume_mismatch": ["Contents did not match the declaration at acceptance. Refused.",
                               "Weighed and measured at acceptance: not what was declared. Refused."],
    "implausible_weight": ["Declared as {goods}, {weight} on the scale. Refused.",
                           "Contents did not match the declaration at acceptance. Refused."],
}
APPROVE_NOTES = {
    "new_shipper_high_value": ["New importer: invoice and import permit checked, shipper visited. Fine to fly.",
                               "New account, but the bank and the consignee confirmed the deal. Approved."],
    "default": ["Called the shipper and checked the invoice. Fine to fly.",
                "Documents checked at the counter. Approved."],
}


def backfill_past_reviews(conn, now: datetime, rng: random.Random | None = None) -> int:
    """Give HIGH bookings whose flights left before `now` the decision a person
    made at the time, so the live review queue only holds flights still to go.

    This is seed tooling, not the engine: it knows which bookings were planted
    and decides the way a careful reviewer would have. Notes follow the rules
    that fired on each booking.
    """
    rng = rng or random.Random(0)
    rows = conn.execute(
        """
        SELECT b.id, b.booked_at, b.lead_time_hours, b.weight_kg, b.commodity_text, s.created_at AS s_created,
               r.rule_hits_json, EXISTS (SELECT 1 FROM seed_truth t WHERE t.booking_id = b.id) AS planted
        FROM booking b JOIN risk_score r ON r.booking_id = b.id JOIN shipper s ON s.id = b.shipper_id
        WHERE b.status = 'in_review'
        """
    ).fetchall()
    n = 0
    for row in rows:
        booked = db.parse_dt(row["booked_at"])
        departure = booked + timedelta(hours=row["lead_time_hours"])
        if departure > now:
            continue  # still to fly: stays in the live review queue
        hits = [h["id"] for h in json.loads(row["rule_hits_json"]) if h.get("kind") == "rule"]
        decided = booked + timedelta(hours=min(row["lead_time_hours"] * rng.uniform(0.2, 0.7), 6))
        if row["planted"]:
            decision = "reject"
            options = next((REJECT_NOTES[h] for h in hits if h in REJECT_NOTES),
                           ["Could not verify the shipper before cut-off. Not accepted."])
        else:
            decision = "approve"
            options = next((APPROVE_NOTES[h] for h in hits if h in APPROVE_NOTES), APPROVE_NOTES["default"])
        age_days = int((booked - db.parse_dt(row["s_created"])).total_seconds() // 86400)
        note = rng.choice(options).format(
            age=("1 day" if age_days == 1 else f"{age_days} days"),
            goods=row["commodity_text"].lower(),
            weight=f"{row['weight_kg']:,.0f} kg")
        conn.execute(
            "INSERT INTO review_decision (booking_id, decision, note, decided_by, decided_at) VALUES (?, ?, ?, ?, ?)",
            (row["id"], decision, note, SEED_REVIEWER, db.iso(decided)))
        conn.execute("UPDATE booking SET status = ? WHERE id = ?",
                     ("rejected" if decision == "reject" else "accepted", row["id"]))
        if decision == "reject":  # refused shipments carry no cover
            conn.execute("UPDATE policy SET status = 'cancelled' WHERE booking_id = ? AND status = 'active'", (row["id"],))
        n += 1
    conn.commit()
    return n


def prune_stale_seed_decisions(conn) -> int:
    """Drop seed-made decisions on bookings that no longer score HIGH (a later
    decision elsewhere changed their score), so every decided booking is HIGH."""
    stale = [r[0] for r in conn.execute(
        "SELECT d.booking_id FROM review_decision d JOIN risk_score r ON r.booking_id = d.booking_id "
        "WHERE d.decided_by = ? AND r.band != 'HIGH'", (SEED_REVIEWER,))]
    for bid in stale:
        conn.execute("DELETE FROM review_decision WHERE booking_id = ? AND decided_by = ?", (bid, SEED_REVIEWER))
        conn.execute("UPDATE booking SET status = 'accepted' WHERE id = ?", (bid,))
    conn.commit()
    return len(stale)


def settle_reviews(conn, model, now: datetime, rng: random.Random | None = None, max_rounds: int = 8) -> None:
    """Score, add past decisions, and repeat until the two agree.

    Decisions feed the score (a rejection flags the shipper, an approval clears
    it), and scores decide which bookings need a decision, so one pass is not
    enough. Each round only removes stale seed decisions or adds missing ones.
    """
    from . import risk

    rng = rng or random.Random(1)
    for _ in range(max_rounds):
        risk.score_all(conn, model)
        changed = prune_stale_seed_decisions(conn) + backfill_past_reviews(conn, now, rng)
        if not changed:
            return
    risk.score_all(conn, model)
    if prune_stale_seed_decisions(conn):
        risk.score_all(conn, model)



def seed_history(conn, summary: dict[str, Any], seed: int, policy_share: float, n_checks: int) -> None:
    """Pre-load past policies and verification checks so the dashboard has a past."""
    from . import policies  # local import: pricing depends on risk output

    from . import graph

    rng = random.Random(seed + 7)
    anchor = db.parse_dt(summary["anchor"])
    flagged = set(graph.flagged_shippers(conn))
    rows = conn.execute(
        """
        SELECT b.*, r.band FROM booking b
        JOIN risk_score r ON r.booking_id = b.id
        JOIN shipper s ON s.id = b.shipper_id
        WHERE r.band IN ('LOW', 'MEDIUM') AND b.status = 'accepted'
          AND s.is_planted_fraud = 0 AND b.id != ?
        """,
        (summary["hero_clean_booking_id"],),
    ).fetchall()
    issued = 0
    for row in rows:
        if db.parse_dt(row["booked_at"]) > anchor - timedelta(days=1) or rng.random() > policy_share:
            continue
        booked = db.parse_dt(row["booked_at"])
        departure = booked + timedelta(hours=row["lead_time_hours"])
        issued_at = min(booked + timedelta(minutes=rng.uniform(5, 360)), departure - timedelta(minutes=20))
        if issued_at <= booked:
            continue
        q = policies.quote_booking(conn, row["id"], as_of=issued_at, flagged=flagged)  # dashboard rules, then
        if not q.bindable:
            continue
        conn.execute(
            "INSERT INTO policy (booking_id, sum_insured_usd, premium_usd, status, qr_token, issued_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (row["id"], q.sum_insured_usd, q.premium_usd, secrets.token_urlsafe(16), db.iso(issued_at)),
        )
        issued += 1

    awbs = [r["awb"] for r in conn.execute("SELECT awb FROM booking").fetchall()]
    prefix = config.awb_prefix()
    reserved = set(fakes.RESERVED_SERIALS) | {a[3:10] for a in awbs}
    for _ in range(n_checks):
        when = anchor - timedelta(minutes=rng.uniform(30, 60 * 24 * 30))
        channel = rng.choices(["chat", "web", "api"], weights=[0.6, 0.25, 0.15])[0]
        u = rng.random()
        if u < 0.80:
            a = rng.choice(awbs)
            conn.execute(
                "INSERT INTO verification_log (channel, input_type, input_value, result, checked_at) VALUES (?, ?, ?, 'GENUINE', ?)",
                (channel, rng.choice(["text", "text", "image"]), awbmod.fmt(a), db.iso(when)))
        elif u < 0.88:
            while True:
                s7 = str(rng.randint(1_000_000, 9_999_999))
                if s7 not in reserved:
                    break
            conn.execute(
                "INSERT INTO verification_log (channel, input_type, input_value, result, checked_at) VALUES (?, ?, ?, 'NOT FOUND', ?)",
                (channel, rng.choice(["image", "image", "text"]), awbmod.fmt(awbmod.make(prefix, s7)), db.iso(when)))
        elif u < 0.93:
            conn.execute(
                "INSERT INTO verification_log (channel, input_type, input_value, result, checked_at) VALUES (?, 'text', ?, 'INVALID FORMAT', ?)",
                (channel, f"unrecognised input ({rng.randint(6, 10)} digits)", db.iso(when)))
        else:
            s7 = str(rng.randint(1_000_000, 9_999_999))
            conn.execute(
                "INSERT INTO verification_log (channel, input_type, input_value, result, checked_at) VALUES (?, 'image', ?, 'SUSPICIOUS', ?)",
                (channel, awbmod.fmt(awbmod.make_invalid(prefix, s7)), db.iso(when)))
    conn.commit()
    summary["history_policies"] = issued


def write_samples(conn, summary: dict[str, Any]) -> list[str]:
    """Genuine AWB images for the clean hero booking and one other genuine booking."""
    from . import documents

    out = []
    ids = [summary["hero_clean_booking_id"]]
    other = conn.execute(
        "SELECT b.id FROM booking b JOIN risk_score r ON r.booking_id = b.id "
        "WHERE r.band = 'LOW' AND b.commodity_class = 'perishables' ORDER BY b.booked_at DESC LIMIT 1"
    ).fetchone()
    if other:
        ids.append(other["id"])
    for bid in ids:
        b = conn.execute(
            "SELECT b.*, s.name AS shipper_name, s.country FROM booking b JOIN shipper s ON s.id = b.shipper_id WHERE b.id = ?",
            (bid,)).fetchone()
        img = documents.draw_awb(
            awbmod.fmt(b["awb"]), b["shipper_name"], COUNTRIES.get(b["country"], ("", "", 0, "", b["country"]))[4],
            "Consignee on file", b["destination"], b["origin"], b["destination"],
            db.parse_dt(b["flight_date"] + "T00:00:00").date(), b["pieces"], b["weight_kg"],
            b["commodity_text"], b["declared_value_usd"], carrier=config.airline_name(),
            note="Synthetic sample of a genuine Demo Airline air waybill (demo data).")
        name = f"genuine_awb_{b['awb']}.png"
        img.save(config.samples_dir() / name, format="PNG", optimize=True)
        out.append(name)
    return out


# ---------------------------------------------------------------- entry point


def run(n_bookings: int | None = None, n_shippers: int | None = None, seed: int | None = None,
        score: bool = True, quiet: bool = False, anchor: datetime | None = None) -> dict[str, Any]:
    cfg = config.settings()["seed"]
    n_bookings = n_bookings or cfg["bookings"]
    # Keep the mix believable at any size: about 7.5 bookings per shipper.
    n_shippers = n_shippers or max(cfg["shippers"], round(n_bookings / 7.5))
    seed = cfg["random_seed"] if seed is None else seed
    anchor = anchor or db.utcnow()

    # Build the database, model and snapshot beside the live ones and swap them in
    # only when everything has worked, so a failed reseed leaves the old demo intact.
    live = config.db_path()
    path = live.with_name(live.name + ".building")
    model_tmp = config.model_path().with_name(config.model_path().name + ".building")
    snap_tmp = config.snapshot_path().with_name(config.snapshot_path().name + ".building")
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), model_tmp, snap_tmp):
        if p.exists():
            p.unlink()
    conn = db.connect(path)
    db.init_schema(conn)

    summary = generate(conn, n_bookings, n_shippers, seed, cfg["planted_fraction"], cfg["history_days"], anchor)

    from . import graph
    graph.rebuild_links(conn)
    summary["fakes"] = [p.name for p in fakes.generate(anchor=anchor)]

    if score:
        from . import anomaly, evaluation, risk
        model = anomaly.train(conn)
        anomaly.save(model, model_tmp)
        risk.score_all(conn, model)
        seed_history(conn, summary, seed, cfg["history_policy_share"], cfg["history_verifications"])
        # Past review decisions for HIGH bookings whose flights have gone, scored
        # until decisions and scores agree. `make train` does the same.
        settle_reviews(conn, model, anchor, random.Random(seed + 11))
        summary["samples"] = write_samples(conn, summary)

    db.set_meta(conn, "seeded_at", db.iso(db.utcnow()))
    db.set_meta(conn, "anchor", db.iso(anchor))
    db.set_meta(conn, "seed_max_booking_id",
                conn.execute("SELECT MAX(id) FROM booking").fetchone()[0])
    if score:
        from . import evaluation
        summary["engine"] = evaluation.engine_report(conn)
    db.set_meta(conn, "demo", {
        "hero_high_booking_id": summary["hero_high_booking_id"],
        "hero_high_awb": summary["hero_high_awb"],
        "hero_clean_booking_id": summary["hero_clean_booking_id"],
        "hero_clean_awb": summary["hero_clean_awb"],
        "ring_shipper_ids": summary["ring_shipper_ids"],
        "demo_fake": fakes.DEMO_FAKE,
        "samples": summary.get("samples", []),
    })
    conn.commit()
    if score:
        db.snapshot(conn, snap_tmp)
    # Copy into the live file with the backup API: works while the app holds it
    # open (Windows will not let us delete it then), and readers see a whole database.
    db.copy_database(conn, live)
    conn.close()
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if p.exists():
            p.unlink()
    if score:
        os.replace(model_tmp, config.model_path())
        os.replace(snap_tmp, config.snapshot_path())
    else:  # unscored data: an old model or snapshot would no longer match it
        for p in (config.model_path(), config.snapshot_path()):
            if p.exists():
                p.unlink()

    if not quiet:
        _print_summary(summary)
    return summary


def _print_summary(s: dict[str, Any]) -> None:
    print(f"Seeded {s['shippers']} shippers and {s['bookings']} bookings "
          f"({s['planted_bookings']} with planted risk) into {config.db_path()}")
    print(f"Fake documents: {len(s['fakes'])} in {config.fakes_dir()}")
    if "engine" in s:
        e = s["engine"]
        print(f"Bands: LOW {e['bands'].get('LOW', 0)}, MEDIUM {e['bands'].get('MEDIUM', 0)}, HIGH {e['bands'].get('HIGH', 0)}")
        print(f"Planted risks caught (MEDIUM or HIGH): {e['planted_caught']}/{e['planted_total']}")
        print(f"False-positive rate on clean bookings: {e['fp_rate_medium_plus']:.1%} at MEDIUM+, "
              f"{e['fp_rate_high']:.1%} at HIGH")
        print(f"Past policies pre-loaded: {s.get('history_policies', 0)}")
    print(f"Demo HIGH booking:  {awbmod.fmt(s['hero_high_awb'])}")
    print(f"Demo clean booking: {awbmod.fmt(s['hero_clean_awb'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cargoshield.seed", description=__doc__.split("\n\n")[0])
    ap.add_argument("--bookings", type=int, default=None, help="number of bookings (default 3000)")
    ap.add_argument("--shippers", type=int, default=None, help="number of shippers (default 400)")
    ap.add_argument("--seed", type=int, default=None, help="random seed (default from settings.yaml)")
    ap.add_argument("--no-score", action="store_true", help="only generate data; skip training and scoring")
    args = ap.parse_args(argv)
    run(args.bookings, args.shippers, args.seed, score=not args.no_score)
    return 0


if __name__ == "__main__":
    sys.exit(main())
