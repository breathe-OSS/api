import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="breathe-tests-")

os.environ["DATABASE_URL"] = ""
os.environ["SENTRY_DSN"] = ""
os.environ["BREATHE_DB_FILE"] = os.path.join(_tmp, "breathe.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.core import database
from app.core import redis_client as redis_module
from app.services import fetchers

TABLES = (
    "sensor_readings",
    "sensor_readings_15m",
    "seasonal_climatology",
    "node_offsets",
    "raw_backfill",
)


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def ping(self):
        return True


@pytest.fixture(autouse=True)
def clean_state():
    conn = database.get_connection()
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()

    fetchers._RAM_CACHE.clear()
    fetchers._NODE_LIVE.clear()
    fetchers._SPIKE_CACHE.clear()
    redis_module.redis_client = None

    yield

    redis_module.redis_client = None


@pytest.fixture
def fake_redis():
    fake = FakeRedis()
    redis_module.redis_client = fake
    return fake


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)
