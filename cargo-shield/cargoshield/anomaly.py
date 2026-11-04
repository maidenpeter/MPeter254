"""Anomaly model (Milestone 2, layer 2).

An IsolationForest over five numeric features (see features.MODEL_FEATURES).
Its output is a percentile: "more unusual than X% of past bookings". The risk
engine turns the top tail of that into a few supporting points. The model is
never the only reason a booking is flagged.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from . import config, db
from .features import MODEL_FEATURE_LABELS, MODEL_FEATURES, booking_features, model_vector


@dataclass
class AnomalyModel:
    forest: IsolationForest
    train_scores: np.ndarray  # sorted raw anomaly scores on the training data
    mean: np.ndarray
    std: np.ndarray
    route_freq: dict[str, float]
    n_train: int
    trained_at: str
    version: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def score(self, feats: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not feats:
            return []
        X = np.array([model_vector(f) for f in feats], dtype=float)
        raw = -self.forest.score_samples(X)  # higher = more unusual
        pct = np.searchsorted(self.train_scores, raw, side="right") / max(len(self.train_scores), 1)
        z = (X - self.mean) / self.std
        out = []
        for i in range(len(feats)):
            j = int(np.argmax(np.abs(z[i])))
            out.append({
                "percentile": float(min(max(pct[i], 0.0), 1.0)),
                "raw": float(raw[i]),
                "driver": MODEL_FEATURES[j],
                "driver_label": MODEL_FEATURE_LABELS[MODEL_FEATURES[j]],
            })
        return out


def route_frequencies(conn: sqlite3.Connection) -> dict[str, float]:
    """Bookings per week on each route over the data window."""
    rows = conn.execute("SELECT origin, destination, COUNT(*) AS n FROM booking GROUP BY origin, destination").fetchall()
    span = conn.execute("SELECT MIN(booked_at) AS lo, MAX(booked_at) AS hi FROM booking").fetchone()
    weeks = 1.0
    if span["lo"] and span["hi"]:
        weeks = max((db.parse_dt(span["hi"]) - db.parse_dt(span["lo"])).total_seconds() / (7 * 86400), 1.0)
    return {f"{r['origin']}-{r['destination']}": r["n"] / weeks for r in rows}


def _all_features(conn: sqlite3.Connection, route_freq: Mapping[str, float]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT b.*, s.created_at AS s_created_at, s.kyc_status AS s_kyc FROM booking b JOIN shipper s ON s.id = b.shipper_id"
    ).fetchall()
    return [
        booking_features(r, {"created_at": r["s_created_at"], "kyc_status": r["s_kyc"]}, route_freq)
        for r in rows
    ]


def train(conn: sqlite3.Connection, random_state: int = 7) -> AnomalyModel:
    route_freq = route_frequencies(conn)
    feats = _all_features(conn, route_freq)
    if len(feats) < 50:
        raise ValueError("need at least 50 bookings to train the anomaly model")
    X = np.array([model_vector(f) for f in feats], dtype=float)
    forest = IsolationForest(n_estimators=300, max_samples=256, contamination="auto", random_state=random_state)
    forest.fit(X)
    raw = np.sort(-forest.score_samples(X))
    std = X.std(axis=0)
    std[std == 0] = 1.0
    return AnomalyModel(
        forest=forest,
        train_scores=raw,
        mean=X.mean(axis=0),
        std=std,
        route_freq=route_freq,
        n_train=len(feats),
        trained_at=db.iso(db.utcnow()),
        meta={"features": MODEL_FEATURES},
    )


def save(model: AnomalyModel, path: str | Path | None = None) -> Path:
    path = Path(path or config.model_path())
    joblib.dump(model, path)
    return path


def load(path: str | Path | None = None) -> AnomalyModel:
    path = Path(path or config.model_path())
    if not path.exists():
        raise FileNotFoundError(f"No anomaly model at {path}. Run the seed (or `python -m cargoshield train`).")
    return joblib.load(path)
