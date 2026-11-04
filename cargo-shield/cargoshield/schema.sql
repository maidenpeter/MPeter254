-- CargoShield demo schema (SQLite).
-- Kept to portable SQL so it moves to Postgres with type changes only.
-- See schema_postgres.sql for the Postgres version of the same tables.
-- Timestamps are ISO-8601 text in SQLite (TIMESTAMPTZ in Postgres).
-- Booleans are 0/1 integers in SQLite (BOOLEAN in Postgres).

CREATE TABLE IF NOT EXISTS shipper (
    id               INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    country          TEXT    NOT NULL,
    phone            TEXT    NOT NULL,
    email            TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    kyc_status       TEXT    NOT NULL CHECK (kyc_status IN ('verified', 'pending', 'failed')),
    is_planted_fraud INTEGER NOT NULL DEFAULT 0  -- ground truth for tests; the engine never reads it
);

CREATE TABLE IF NOT EXISTS booking (
    id                 INTEGER PRIMARY KEY,
    awb                TEXT    NOT NULL UNIQUE,
    shipper_id         INTEGER NOT NULL REFERENCES shipper (id),
    origin             TEXT    NOT NULL,
    destination        TEXT    NOT NULL,
    flight_date        TEXT    NOT NULL,
    commodity_text     TEXT    NOT NULL,
    commodity_class    TEXT    NOT NULL,
    pieces             INTEGER NOT NULL,
    weight_kg          REAL    NOT NULL,
    volume_m3          REAL    NOT NULL,
    declared_value_usd REAL    NOT NULL,
    payment_method     TEXT    NOT NULL,
    payment_ref        TEXT,   -- payer account reference; feeds the fraud graph
    booked_at          TEXT    NOT NULL,
    lead_time_hours    REAL    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'accepted', 'in_review', 'on_hold', 'rejected'))
);

CREATE TABLE IF NOT EXISTS risk_score (
    booking_id     INTEGER PRIMARY KEY REFERENCES booking (id),
    score          INTEGER NOT NULL,
    band           TEXT    NOT NULL CHECK (band IN ('LOW', 'MEDIUM', 'HIGH')),
    reasons_json   TEXT    NOT NULL,
    rule_hits_json TEXT    NOT NULL,
    anomaly_score  REAL,
    scored_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS policy (
    id              INTEGER PRIMARY KEY,
    booking_id      INTEGER NOT NULL REFERENCES booking (id),
    sum_insured_usd REAL    NOT NULL,
    premium_usd     REAL    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('active', 'cancelled')),
    qr_token        TEXT    NOT NULL UNIQUE,
    issued_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_log (
    id          INTEGER PRIMARY KEY,
    channel     TEXT NOT NULL,
    input_type  TEXT NOT NULL,
    input_value TEXT,
    result      TEXT NOT NULL,
    checked_at  TEXT NOT NULL
);

-- Generic edge list for the fraud graph: (shipper, 17) -> (phone, +254 0...)
CREATE TABLE IF NOT EXISTS contact_link (
    a_type  TEXT NOT NULL,
    a_value TEXT NOT NULL,
    b_type  TEXT NOT NULL,
    b_value TEXT NOT NULL,
    PRIMARY KEY (a_type, a_value, b_type, b_value)
);

-- Additions beyond the spec's data model ------------------------------------

-- Human decisions on the review queue. Nothing is auto-rejected.
CREATE TABLE IF NOT EXISTS review_decision (
    id         INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES booking (id),
    decision   TEXT    NOT NULL CHECK (decision IN ('approve', 'hold', 'reject')),
    note       TEXT,
    decided_by TEXT    NOT NULL,
    decided_at TEXT    NOT NULL
);

-- Which planted pattern(s) each seeded risky booking carries. Tests only.
CREATE TABLE IF NOT EXISTS seed_truth (
    booking_id INTEGER NOT NULL REFERENCES booking (id),
    pattern    TEXT    NOT NULL,
    PRIMARY KEY (booking_id, pattern)
);

-- Small key/value store: seed time, demo shortcuts, etc.
CREATE TABLE IF NOT EXISTS demo_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_booking_shipper   ON booking (shipper_id);
CREATE INDEX IF NOT EXISTS idx_booking_booked_at ON booking (booked_at);
CREATE INDEX IF NOT EXISTS idx_risk_band         ON risk_score (band);
CREATE INDEX IF NOT EXISTS idx_policy_booking    ON policy (booking_id);
CREATE INDEX IF NOT EXISTS idx_verif_checked     ON verification_log (checked_at);
CREATE INDEX IF NOT EXISTS idx_link_b            ON contact_link (b_type, b_value);
CREATE INDEX IF NOT EXISTS idx_review_booking    ON review_decision (booking_id);
