"""Configuration loading.

Settings live in YAML under config/. Paths can be overridden with environment
variables so tests (and a second demo copy) can use their own data folder:

    CARGOSHIELD_DATA_DIR    where the database, model and generated documents go
    CARGOSHIELD_CONFIG_DIR  where settings.yaml, rules.yaml and commodities.yaml live
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return Path(os.environ.get("CARGOSHIELD_CONFIG_DIR", PROJECT_ROOT / "config"))


def _load_yaml(name: str) -> dict[str, Any]:
    with open(config_dir() / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=None)
def settings() -> dict[str, Any]:
    return _load_yaml("settings.yaml")


@lru_cache(maxsize=None)
def rules_config() -> dict[str, Any]:
    return _load_yaml("rules.yaml")


@lru_cache(maxsize=None)
def commodities() -> dict[str, Any]:
    return _load_yaml("commodities.yaml")


def reload() -> None:
    """Forget cached config (used by tests and after editing YAML at runtime)."""
    settings.cache_clear()
    rules_config.cache_clear()
    commodities.cache_clear()


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def data_dir() -> Path:
    env = os.environ.get("CARGOSHIELD_DATA_DIR")
    d = Path(env) if env else _resolve(settings()["paths"]["data_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def routes_csv() -> Path:
    return _resolve(settings()["paths"]["routes_csv"])


def db_path() -> Path:
    return data_dir() / "cargoshield.db"


def snapshot_path() -> Path:
    """Copy of the database taken right after seeding. `reset` restores it."""
    return data_dir() / "demo_start.db"


def model_path() -> Path:
    return data_dir() / "anomaly_model.joblib"


def fakes_dir() -> Path:
    d = data_dir() / "fakes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def samples_dir() -> Path:
    """Genuine sample documents for real bookings (made at seed time)."""
    d = data_dir() / "samples"
    d.mkdir(parents=True, exist_ok=True)
    return d


def awb_prefix() -> str:
    return str(settings()["airline"]["awb_prefix"]).zfill(3)


def airline_name() -> str:
    return settings()["airline"]["name"]
