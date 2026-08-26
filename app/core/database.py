# SPDX-License-Identifier: MIT
#
# Copyright (C) 2026 The Breathe Open Source Project
# Copyright (C) 2026 sidharthify <wednisegit@gmail.com>
# Copyright (C) 2026 FlashWreck <theghost3370@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# the store. sqlite locally, postgres in production, behind one set of functions.
#
# the two dialects differ in three places and nowhere else: the parameter marker
# is %s against ?, the autoincrement column is spelled differently, and postgres
# can hand out a server side cursor for streaming. _is_postgres and _placeholder
# are what keep that from spreading through every query.

import math
import os
import sqlite3
import time
from typing import Dict, Iterator, List

import psycopg2
from psycopg2.extras import RealDictCursor

from app.core.config import NODES_CONFIG

# only the particulates get homogenised. temperature and humidity are not
# averaged across sensors in a way that a per sensor offset would help with.
HOMOGENISED_METRICS = ("pm2_5", "pm10")

# the pseudo sensor name under which the correction for pre per-sensor history
# is stored.
OFFSET_LEGACY_KEY = "__legacy__"

# offsets older than this get recomputed by the background loop.
OFFSET_MAX_AGE = 86400

# how much history the offset fit looks at.
OFFSET_HISTORY_DAYS = 180

# a sensor needs this many 15 minute buckets before it is worth fitting. 96 is
# one full day.
OFFSET_MIN_SAMPLES = 96

OFFSET_MAX_ITERATIONS = 100
OFFSET_TOLERANCE = 1e-10

# what a caller may ask for, mapped to the column that holds it.
VALID_METRICS = {
    "pm2.5": "pm2_5",
    "pm10": "pm10",
    "temp": "temp",
    "humidity": "humidity",
}

DEFAULT_METRICS = ["pm2_5", "pm10"]

# the rollup table stores 15 minute buckets, so it can serve any interval that
# is a whole number of them.
ROLLUP_INTERVAL = 900

# rows pulled per round trip while streaming.
STREAM_CHUNK = 1000

DB_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "breathe.db"
)


def get_connection():
    """Open a connection to postgres if DATABASE_URL is set, otherwise to the local sqlite file."""
    db_url = os.getenv("DATABASE_URL")

    if db_url:
        return psycopg2.connect(db_url, cursor_factory=RealDictCursor)

    conn = sqlite3.connect(DB_FILE)

    # so rows come back subscriptable by column name, matching what
    # RealDictCursor gives us on the postgres side.
    conn.row_factory = sqlite3.Row
    return conn


def _is_postgres(conn) -> bool:
    """Tell the two backends apart. Only psycopg2 connections carry a dsn."""
    return hasattr(conn, "dsn")


def _placeholder(is_pg: bool) -> str:
    """The parameter marker for this backend."""
    return "%s" if is_pg else "?"


def _close_quietly(cursor, conn, is_pg: bool) -> None:
    """Tear a streaming query down without letting cleanup raise over the real error."""
    # cursor is None when the failure happened before one could be opened.
    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            pass

    # a named cursor leaves the connection mid transaction, and closing it in
    # that state leaks the server side portal.
    if is_pg:
        try:
            conn.rollback()
        except Exception:
            pass

    try:
        conn.close()
    except Exception:
        pass


def check_postgres_health() -> bool:
    """Round trip one query. Returns False rather than raising, so /health can report it."""
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT 1;")
        c.fetchone()
        return True

    except Exception:
        return False

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def init_db() -> None:
    """Create the tables and indexes if they are not there yet. Runs on import."""
    conn = get_connection()
    c = conn.cursor()
    is_pg = _is_postgres(conn)

    # the schema is identical across both backends apart from how the
    # autoincrementing key is spelled, so it is written once.
    primary_key = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id {primary_key},
            zone_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            pm2_5 REAL,
            pm10 REAL,
            temp REAL,
            humidity REAL,
            UNIQUE(zone_id, timestamp)
        )
    """)

    # temp and humidity were added after the table existed, so older deployments
    # need them bolting on.
    if is_pg:
        c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='sensor_readings'"
        )
        columns = [row["column_name"] for row in c.fetchall()]
    else:
        c.execute("PRAGMA table_info(sensor_readings)")
        columns = [row[1] for row in c.fetchall()]

    if "temp" not in columns:
        c.execute("ALTER TABLE sensor_readings ADD COLUMN temp REAL")

    if "humidity" not in columns:
        c.execute("ALTER TABLE sensor_readings ADD COLUMN humidity REAL")

    # pre-averaged 15 minute buckets, so a year long chart does not have to
    # aggregate a year of raw rows on every request.
    c.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings_15m (
            zone_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            pm2_5 REAL,
            pm10 REAL,
            temp REAL,
            humidity REAL,
            UNIQUE(zone_id, ts)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS seasonal_climatology (
            zone_id TEXT NOT NULL,
            month INTEGER NOT NULL,
            pm2_5 REAL,
            pm10 REAL,
            precipitation REAL,
            temp REAL,
            updated_at REAL,
            UNIQUE(zone_id, month)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS node_offsets (
            zone_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            factor REAL,
            samples INTEGER,
            updated_at REAL,
            UNIQUE(zone_id, node_name, metric)
        )
    """)

    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_zone_time ON sensor_readings (zone_id, timestamp)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_zone_time_15m ON sensor_readings_15m (zone_id, ts)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_node_offsets ON node_offsets (zone_id)")

    conn.commit()
    conn.close()


def save_reading(zone_id, pm25, pm10, temp=None, humidity=None, timestamp=None) -> None:
    """Store one reading. Callers with more than one should use save_readings."""
    if timestamp is None:
        timestamp = time.time()

    save_readings([{
        "zone_id": zone_id,
        "timestamp": timestamp,
        "pm2_5": pm25,
        "pm10": pm10,
        "temp": temp,
        "humidity": humidity,
    }])


def save_readings(readings: List[dict]) -> None:
    """Store a batch of readings in one transaction, ignoring ones we already hold."""
    if not readings:
        return

    conn = get_connection()
    c = conn.cursor()
    is_pg = _is_postgres(conn)
    marker = _placeholder(is_pg)

    # a duplicate (zone_id, timestamp) means we polled twice inside one sensor
    # reporting period, which is expected and not worth an error.
    conflict = (
        "ON CONFLICT (zone_id, timestamp) DO NOTHING"
        if is_pg
        else "ON CONFLICT(zone_id, timestamp) DO NOTHING"
    )

    try:
        c.executemany(
            f"""
                INSERT INTO sensor_readings
                    (zone_id, timestamp, pm2_5, pm10, temp, humidity)
                VALUES ({marker}, {marker}, {marker}, {marker}, {marker}, {marker})
                {conflict}
            """,
            [
                (
                    r["zone_id"],
                    r["timestamp"],
                    r["pm2_5"],
                    r["pm10"],
                    r.get("temp"),
                    r.get("humidity"),
                )
                for r in readings
            ],
        )
        conn.commit()

    except Exception as e:
        # losing a poll is survivable, the next one is fifteen minutes away.
        print(f"DB Batch Save Error: {e}")

    finally:
        conn.close()


def get_history(zone_id, hours=24) -> List[dict]:
    """Every raw reading for a zone in the last so many hours, oldest first."""
    conn = get_connection()
    c = conn.cursor()
    marker = _placeholder(_is_postgres(conn))

    cutoff = time.time() - (hours * 3600)

    c.execute(
        f"""
            SELECT timestamp as ts, pm2_5, pm10, temp, humidity
            FROM sensor_readings
            WHERE zone_id = {marker} AND timestamp > {marker}
            ORDER BY timestamp ASC
        """,
        (zone_id, cutoff),
    )

    rows = c.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def save_seasonal_climatology(zone_id, months: List[dict]) -> None:
    """Replace a zone's stored monthly normals with a freshly computed set."""
    if not months:
        return

    conn = get_connection()
    c = conn.cursor()
    is_pg = _is_postgres(conn)
    marker = _placeholder(is_pg)

    try:
        c.executemany(
            f"""
                INSERT INTO seasonal_climatology
                    (zone_id, month, pm2_5, pm10, precipitation, temp, updated_at)
                VALUES ({marker}, {marker}, {marker}, {marker}, {marker}, {marker}, {marker})
                ON CONFLICT (zone_id, month) DO UPDATE SET
                    pm2_5 = excluded.pm2_5,
                    pm10 = excluded.pm10,
                    precipitation = excluded.precipitation,
                    temp = excluded.temp,
                    updated_at = excluded.updated_at
            """,
            [
                (
                    zone_id,
                    m["month"],
                    m["pm2_5"],
                    m["pm10"],
                    m["precipitation"],
                    m["temp"],
                    m["updated_at"],
                )
                for m in months
            ],
        )
        conn.commit()

    except Exception as e:
        print(f"DB Climatology Save Error: {e}")

    finally:
        conn.close()


def get_seasonal_climatology(zone_id) -> List[dict]:
    """A zone's twelve monthly normals, in month order."""
    conn = get_connection()
    c = conn.cursor()
    marker = _placeholder(_is_postgres(conn))

    c.execute(
        f"""
            SELECT month, pm2_5, pm10, precipitation, temp, updated_at
            FROM seasonal_climatology
            WHERE zone_id = {marker}
            ORDER BY month ASC
        """,
        (zone_id,),
    )

    rows = c.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_monthly_sensor_averages(zone_id) -> Dict[int, dict]:
    """Our own readings averaged per calendar month, with a count so callers can judge them."""
    conn = get_connection()
    c = conn.cursor()
    is_pg = _is_postgres(conn)
    marker = _placeholder(is_pg)

    # extracting a month from a unix timestamp is the one thing the two engines
    # spell completely differently.
    if is_pg:
        month_expr = "CAST(EXTRACT(MONTH FROM to_timestamp(timestamp)) AS INTEGER)"
    else:
        month_expr = "CAST(strftime('%m', timestamp, 'unixepoch') AS INTEGER)"

    c.execute(
        f"""
            SELECT {month_expr} as month,
                   AVG(pm2_5) as pm2_5, AVG(pm10) as pm10, COUNT(*) as samples
            FROM sensor_readings
            WHERE zone_id = {marker}
            GROUP BY 1
        """,
        (zone_id,),
    )

    rows = c.fetchall()
    conn.close()

    return {row["month"]: dict(row) for row in rows}


def refresh_15m_rollups() -> None:
    """Rebuild the 15 minute rollup table from the raw readings.

    Called from the background loop after every fetch pass. This is a blocking
    call, so the caller runs it in a thread.
    """
    conn = get_connection()
    c = conn.cursor()

    try:
        c.execute(f"""
            INSERT INTO sensor_readings_15m (zone_id, ts, pm2_5, pm10, temp, humidity)
            SELECT
                zone_id,
                CAST(timestamp / {ROLLUP_INTERVAL} AS INTEGER) * {ROLLUP_INTERVAL} as ts,
                AVG(pm2_5), AVG(pm10), AVG(temp), AVG(humidity)
            FROM sensor_readings
            GROUP BY zone_id, CAST(timestamp / {ROLLUP_INTERVAL} AS INTEGER) * {ROLLUP_INTERVAL}
            ON CONFLICT (zone_id, ts) DO UPDATE SET
                pm2_5 = excluded.pm2_5,
                pm10 = excluded.pm10,
                temp = excluded.temp,
                humidity = excluded.humidity
        """)
        conn.commit()

    except Exception as e:
        print(f"DB Rollup Error: {e}")

    finally:
        conn.close()


def zone_node_names(zone_id: str) -> List[str]:
    """Names of the enabled sensors configured for a zone, empty if it has none."""
    entry = NODES_CONFIG.get(zone_id)
    if not entry:
        return []

    return [node["name"] for node in entry.get("nodes", []) if node.get("enabled", True)]


def _fetch_node_columns(zone_id: str, node_names: List[str], days: int) -> dict:
    """Pull recent per sensor readings for a zone, keyed by metric then sensor then bucket.

    Values are returned as natural logs. Working in logs keeps a single filthy
    day from dominating the offset estimate, since the model we are fitting is
    multiplicative: one sensor reads a roughly constant percentage above
    another, not a constant number of micrograms above it.
    """
    conn = get_connection()
    c = conn.cursor()
    is_pg = _is_postgres(conn)
    marker = _placeholder(is_pg)

    cutoff = time.time() - (days * 86400)
    ids = [f"{zone_id}_{name}" for name in node_names]
    slots = ", ".join([marker] * len(ids))

    columns = {metric: {name: {} for name in node_names} for metric in HOMOGENISED_METRICS}

    try:
        c.execute(
            f"""
                SELECT zone_id, ts, pm2_5, pm10
                FROM sensor_readings_15m
                WHERE ts > {marker} AND zone_id IN ({slots})
            """,
            [cutoff] + ids,
        )

        for row in c.fetchall():
            data = dict(row)

            # rows are stored as "<zone>_<sensor name>", so the sensor name is
            # whatever follows the zone id and its separator.
            name = data["zone_id"][len(zone_id) + 1:]
            if name not in columns[HOMOGENISED_METRICS[0]]:
                continue

            for metric in HOMOGENISED_METRICS:
                value = data.get(metric)

                # log(0) is undefined and a zero reading is a sensor fault
                # anyway, so those buckets are dropped rather than clamped.
                if value is not None and value > 0:
                    columns[metric][name][int(data["ts"])] = math.log(value)

    except Exception as e:
        print(f"DB Offset Read Error: {e}")

    finally:
        conn.close()

    return columns


def _solve_offsets(column: dict) -> dict:
    """Estimate how each sensor sits against the rest of the network for one metric.

    Sensors come and go, so a plain average per sensor is not comparable: they
    covered different stretches of time with different weather. This solves the
    level and the offsets together by alternating least squares:

        1. assume every offset is zero
        2. per bucket, take the level from whichever sensors reported, net of offsets
        3. re-estimate each offset as its mean gap from that level
        4. re-centre so the offsets average out
        5. repeat until nothing moves

    Returns multiplicative factors normalised to average 1.0, so a zone with
    every sensor reporting keeps the value it already had. Empty if fewer than
    two sensors clear OFFSET_MIN_SAMPLES.
    """
    active = {name: series for name, series in column.items() if len(series) >= OFFSET_MIN_SAMPLES}
    if len(active) < 2:
        return {}

    stamps = set()
    for series in active.values():
        stamps.update(series)

    # which sensors reported in each bucket, worked out once rather than per
    # iteration.
    members = {stamp: [name for name in active if stamp in active[name]] for stamp in stamps}

    offsets = {name: 0.0 for name in active}

    for _ in range(OFFSET_MAX_ITERATIONS):
        levels = {}
        for stamp in stamps:
            present = members[stamp]
            total = 0.0
            for name in present:
                total += active[name][stamp] - offsets[name]
            levels[stamp] = total / len(present)

        updated = {}
        for name, series in active.items():
            total = 0.0
            for stamp, value in series.items():
                total += value - levels[stamp]
            updated[name] = total / len(series)

        # the level and the offsets are only defined up to a constant, so the
        # offsets are pinned to average zero to keep the fit from drifting.
        centre = sum(updated.values()) / len(updated)
        for name in updated:
            updated[name] -= centre

        shift = max(abs(updated[name] - offsets[name]) for name in updated)
        offsets = updated

        if shift < OFFSET_TOLERANCE:
            break

    factors = {name: math.exp(value) for name, value in offsets.items()}

    average = sum(factors.values()) / len(factors)
    if average <= 0:
        return {}

    return {name: factors[name] / average for name in factors}


def _rescale_arithmetic(column: dict, factors: dict) -> dict:
    """Correct the log space factors so they also balance as ordinary averages.

    The offsets are fitted on logs but the zone series is an arithmetic mean,
    and the two do not agree: a noisier sensor has a higher arithmetic mean than
    a quieter one at the same log mean. Left alone that leaks a systematic bias
    back in when a sensor drops out. One rescaling pass over the buckets where
    every sensor reported cut the leftover bias from 1.12% to 0.27% on Jammu.

    Falls back to the input factors when there is not enough overlap to measure.
    """
    names = sorted(factors)

    # only buckets where every sensor reported can be compared like for like.
    shared = None
    for name in names:
        stamps = set(column.get(name) or {})
        shared = stamps if shared is None else shared & stamps

    if not shared or len(shared) < OFFSET_MIN_SAMPLES:
        return factors

    ratios = {}
    for name in names:
        total = 0.0
        for stamp in shared:
            total += math.exp(column[name][stamp]) / factors[name]
        ratios[name] = total / len(shared)

    average = sum(ratios.values()) / len(ratios)
    if average <= 0:
        return factors

    scaled = {name: factors[name] * ratios[name] / average for name in names}

    centre = sum(scaled.values()) / len(scaled)
    if centre <= 0:
        return factors

    return {name: scaled[name] / centre for name in scaled}


def _legacy_factor(column: dict, factors: dict) -> float:
    """Correction for buckets predating per sensor storage, where only a zone row exists.

    Per sensor saving was added after zone saving, so the earliest history
    cannot be decomposed. The best available guess is that those rows came from
    whichever sensors were running in the first week we do have sensor data for.
    This is an inference rather than a measurement, and it is the weakest part
    of the series.
    """
    earliest = None
    for name in factors:
        series = column.get(name) or {}
        if not series:
            continue

        first = min(series)
        if earliest is None or first < earliest:
            earliest = first

    if earliest is None:
        return 1.0

    window = earliest + (7 * 86400)

    present = [
        name
        for name in factors
        if (column.get(name) or {}) and min(column[name]) <= window
    ]

    if not present:
        return 1.0

    return sum(factors[name] for name in present) / len(present)


def compute_node_offsets(zone_id: str) -> List[dict]:
    """Work out the per sensor offsets for a zone, ready to persist.

    Returns nothing for single sensor zones, where there is no composition to
    correct for and the raw average is already consistent.
    """
    node_names = zone_node_names(zone_id)
    if len(node_names) < 2:
        return []

    columns = _fetch_node_columns(zone_id, node_names, OFFSET_HISTORY_DAYS)
    now = time.time()
    rows = []

    for metric in HOMOGENISED_METRICS:
        column = columns[metric]

        factors = _solve_offsets(column)
        if not factors:
            continue

        factors = _rescale_arithmetic(column, factors)

        for name, factor in factors.items():
            rows.append({
                "zone_id": zone_id,
                "node_name": name,
                "metric": metric,
                "factor": factor,
                "samples": len(column[name]),
                "updated_at": now,
            })

        rows.append({
            "zone_id": zone_id,
            "node_name": OFFSET_LEGACY_KEY,
            "metric": metric,
            "factor": _legacy_factor(column, factors),
            "samples": 0,
            "updated_at": now,
        })

    return rows


def save_node_offsets(rows: List[dict]) -> None:
    """Upsert computed sensor offsets, replacing any earlier values for the zone."""
    if not rows:
        return

    conn = get_connection()
    c = conn.cursor()
    is_pg = _is_postgres(conn)
    marker = _placeholder(is_pg)

    try:
        c.executemany(
            f"""
                INSERT INTO node_offsets
                    (zone_id, node_name, metric, factor, samples, updated_at)
                VALUES ({marker}, {marker}, {marker}, {marker}, {marker}, {marker})
                ON CONFLICT (zone_id, node_name, metric) DO UPDATE SET
                    factor = excluded.factor,
                    samples = excluded.samples,
                    updated_at = excluded.updated_at
            """,
            [
                (
                    r["zone_id"],
                    r["node_name"],
                    r["metric"],
                    r["factor"],
                    r["samples"],
                    r["updated_at"],
                )
                for r in rows
            ],
        )
        conn.commit()

    except Exception as e:
        print(f"DB Offset Save Error: {e}")

    finally:
        conn.close()


def get_node_offsets(zone_id: str) -> dict:
    """Stored offsets for a zone as {metric: {sensor_name: factor}}, empty if never computed."""
    conn = get_connection()
    c = conn.cursor()
    marker = _placeholder(_is_postgres(conn))

    result = {}
    try:
        c.execute(
            f"""
                SELECT node_name, metric, factor
                FROM node_offsets
                WHERE zone_id = {marker}
            """,
            (zone_id,),
        )

        for row in c.fetchall():
            data = dict(row)
            factor = data.get("factor")

            # a zero or negative factor would be divided by later, and cannot
            # mean anything sensible.
            if factor and factor > 0:
                result.setdefault(data["metric"], {})[data["node_name"]] = factor

    except Exception as e:
        print(f"DB Offset Lookup Error: {e}")

    finally:
        conn.close()

    return result


def refresh_stale_node_offsets() -> None:
    """Recompute sensor offsets for any zone whose stored values are older than a day.

    Offsets move slowly, so this is cheap to skip. It does need to run after a
    new sensor is installed, otherwise that sensor is averaged in uncorrected
    and reintroduces exactly the step this is here to remove.
    """
    conn = get_connection()
    c = conn.cursor()

    fresh = set()
    try:
        c.execute(
            "SELECT zone_id, MAX(updated_at) as updated_at FROM node_offsets GROUP BY zone_id"
        )

        for row in c.fetchall():
            data = dict(row)
            if data.get("updated_at") and (time.time() - data["updated_at"]) < OFFSET_MAX_AGE:
                fresh.add(data["zone_id"])

    except Exception as e:
        print(f"DB Offset Freshness Error: {e}")

    finally:
        conn.close()

    for zone_id in NODES_CONFIG:
        if zone_id in fresh:
            continue

        if len(zone_node_names(zone_id)) < 2:
            continue

        save_node_offsets(compute_node_offsets(zone_id))


def _resolve_metrics(metrics: List[str]) -> List[str]:
    """Map requested metric names onto columns, falling back to the particulates."""
    selected = [VALID_METRICS[m] for m in metrics if m in VALID_METRICS]
    return selected or list(DEFAULT_METRICS)


def _pick_table(interval_sec: int) -> tuple:
    """Choose the rollup table when the interval allows it. Returns (table, time column)."""
    if interval_sec >= ROLLUP_INTERVAL and interval_sec % ROLLUP_INTERVAL == 0:
        return "sensor_readings_15m", "ts"

    return "sensor_readings", "timestamp"


def _stream_homogenised(
    location,
    node_names,
    offsets,
    cutoff,
    interval_sec,
    selected_metrics,
    table,
    time_col,
) -> Iterator[dict]:
    """Stream a zone series rebuilt from its sensors, with per sensor offsets divided out.

    For each bucket the value is the mean over the sensors that actually
    reported, of reading / factor. Dividing by the factor restores a sensor to
    network scale, so the series does not step when one joins or drops out.
    Buckets with no sensor rows at all fall back to the stored zone row scaled
    by the legacy factor.

    Rows arrive ordered by bucket, so this accumulates and flushes one bucket at
    a time and never holds the full series in memory.
    """
    conn = get_connection()
    is_pg = _is_postgres(conn)
    c = None

    try:
        # a named cursor keeps the result set on the postgres server instead of
        # pulling a year of rows into this process.
        c = conn.cursor(name="homogenised_data_cursor") if is_pg else conn.cursor()

        marker = _placeholder(is_pg)
        ids = [location] + [f"{location}_{name}" for name in node_names]
        slots = ", ".join([marker] * len(ids))
        metrics_sql = ", ".join([f"AVG({m}) as {m}" for m in selected_metrics])
        ts_expr = f"CAST({time_col} / {interval_sec} AS INTEGER) * {interval_sec}"

        c.execute(
            f"""
                SELECT
                    zone_id,
                    {ts_expr} as ts,
                    {metrics_sql}
                FROM {table}
                WHERE {time_col} > {marker} AND zone_id IN ({slots})
                GROUP BY zone_id, 2
                ORDER BY 2 ASC, zone_id ASC
            """,
            [cutoff] + ids,
        )

        current_ts = None
        node_totals = {}
        node_counts = {}
        zone_row = {}

        def flush():
            """Emit the bucket accumulated so far, preferring sensor rows over the stored zone row."""
            out = {"zone_id": location, "ts": current_ts}

            for metric in selected_metrics:
                value = None

                if node_counts.get(metric):
                    value = node_totals[metric] / node_counts[metric]

                elif zone_row.get(metric) is not None:
                    legacy = offsets.get(metric, {}).get(OFFSET_LEGACY_KEY, 1.0)
                    value = zone_row[metric] / legacy

                if value is not None:
                    out[metric] = round(value, 2)

            return out

        while True:
            rows = c.fetchmany(STREAM_CHUNK)
            if not rows:
                break

            for row in rows:
                data = dict(row)
                bucket = int(data["ts"])

                # the query is ordered by bucket, so a new one means the
                # previous is complete.
                if current_ts is not None and bucket != current_ts:
                    yield flush()
                    node_totals = {}
                    node_counts = {}
                    zone_row = {}

                current_ts = bucket

                zone_id = data["zone_id"]
                if zone_id == location:
                    for metric in selected_metrics:
                        zone_row[metric] = data.get(metric)
                    continue

                name = zone_id[len(location) + 1:]
                for metric in selected_metrics:
                    value = data.get(metric)
                    if value is None:
                        continue

                    factor = offsets.get(metric, {}).get(name, 1.0)
                    if factor <= 0:
                        continue

                    node_totals[metric] = node_totals.get(metric, 0.0) + (value / factor)
                    node_counts[metric] = node_counts.get(metric, 0) + 1

        # the last bucket has nothing after it to trigger the flush above.
        if current_ts is not None:
            yield flush()

    except Exception as e:
        print(f"DB Homogenised Stream Error: {e}")

    finally:
        _close_quietly(c, conn, is_pg)


def stream_historical_data(
    location: str,
    time_range_sec: int,
    interval_sec: int,
    metrics: List[str],
) -> Iterator[dict]:
    """Stream readings over a window, grouped into interval sized buckets.

    For multi sensor zones the series is rebuilt from the individual sensor rows
    with per sensor offsets applied, so that a sensor joining or dropping out of
    the zone does not show up as a change in air quality.
    """
    selected_metrics = _resolve_metrics(metrics)
    table, time_col = _pick_table(interval_sec)
    cutoff = time.time() - time_range_sec

    # "all" is every zone at once, which cannot be homogenised because the
    # sensors belong to different zones.
    if location != "all":
        node_names = zone_node_names(location)

        if len(node_names) >= 2:
            offsets = get_node_offsets(location)

            # without stored offsets there is nothing to correct with, so fall
            # through to the plain query rather than serving a stepped series
            # dressed up as a corrected one.
            if offsets:
                yield from _stream_homogenised(
                    location,
                    node_names,
                    offsets,
                    cutoff,
                    interval_sec,
                    selected_metrics,
                    table,
                    time_col,
                )
                return

    conn = get_connection()
    is_pg = _is_postgres(conn)
    c = None

    try:
        c = conn.cursor(name="historical_data_cursor") if is_pg else conn.cursor()

        marker = _placeholder(is_pg)
        metrics_sql = ", ".join([f"AVG({m}) as {m}" for m in selected_metrics])
        ts_expr = f"CAST({time_col} / {interval_sec} AS INTEGER) * {interval_sec}"

        where_clause = f"{time_col} > {marker}"
        params = [cutoff]

        if location != "all":
            where_clause += f" AND zone_id = {marker}"
            params.append(location)

        c.execute(
            f"""
                SELECT
                    zone_id,
                    {ts_expr} as ts,
                    {metrics_sql}
                FROM {table}
                WHERE {where_clause}
                GROUP BY zone_id, 2
                ORDER BY 2 ASC, zone_id ASC
            """,
            params,
        )

        while True:
            rows = c.fetchmany(STREAM_CHUNK)
            if not rows:
                break

            for row in rows:
                d = dict(row)

                for m in selected_metrics:
                    if d.get(m) is not None:
                        d[m] = round(d[m], 2)

                yield d

    except Exception as e:
        print(f"DB Stream Error: {e}")

    finally:
        _close_quietly(c, conn, is_pg)


init_db()
