"""Shared fixtures. The whole suite runs against one freshly seeded demo
database in a temporary folder, so it never touches data/ in the repo."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("cargoshield-data")
    os.environ["CARGOSHIELD_DATA_DIR"] = str(d)
    from cargoshield import config

    config.reload()
    return d


@pytest.fixture(scope="session")
def seeded(data_dir) -> dict:
    from cargoshield import seed

    return seed.run(n_bookings=3000, quiet=True)


@pytest.fixture()
def conn(seeded):
    from cargoshield import db

    db.restore()  # start from the demo start state, whatever earlier tests changed
    c = db.connect()
    yield c
    c.close()


@pytest.fixture(scope="session")
def model(seeded):
    from cargoshield import anomaly

    return anomaly.load()


@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient

    from cargoshield import db
    from cargoshield.app import app

    db.restore()  # every API test starts from the demo start state
    with TestClient(app, base_url="http://localhost") as c:
        yield c
