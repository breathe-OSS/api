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

import sqlite3
import time
import os
import math
import psycopg2
from psycopg2.extras import RealDictCursor

from app.core.config import NODES_CONFIG

HOMOGENISED_METRICS = ('pm2_5', 'pm10')
OFFSET_LEGACY_KEY = '__legacy__'
OFFSET_MAX_AGE = 86400
OFFSET_HISTORY_DAYS = 180
OFFSET_MIN_SAMPLES = 96
OFFSET_MAX_ITERATIONS = 100
OFFSET_TOLERANCE = 1e-10

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "breathe.db")

def get_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
        return conn
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def check_postgres_health() -> bool:
    """Check connection health for PostgreSQL"""
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

def init_db():
    conn = get_connection()
    c = conn.cursor()
    # Check if we are connected to Postgres
    is_pg = hasattr(conn, 'dsn')

    if is_pg:
        c.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id SERIAL PRIMARY KEY,
                zone_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                pm2_5 REAL,
                pm10 REAL,
                temp REAL,
                humidity REAL,
                UNIQUE(zone_id, timestamp)
            )
        ''')
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sensor_readings'")
        columns = [row['column_name'] for row in c.fetchall()]
        if 'temp' not in columns:
            c.execute('ALTER TABLE sensor_readings ADD COLUMN temp REAL')
        if 'humidity' not in columns:
            c.execute('ALTER TABLE sensor_readings ADD COLUMN humidity REAL')
            
        c.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings_15m (
                zone_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                pm2_5 REAL,
                pm10 REAL,
                temp REAL,
                humidity REAL,
                UNIQUE(zone_id, ts)
            )
        ''')

        c.execute('''
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
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS node_offsets (
                zone_id TEXT NOT NULL,
                node_name TEXT NOT NULL,
                metric TEXT NOT NULL,
                factor REAL,
                samples INTEGER,
                updated_at REAL,
                UNIQUE(zone_id, node_name, metric)
            )
        ''')
    else:
        c.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                pm2_5 REAL,
                pm10 REAL,
                temp REAL,
                humidity REAL,
                UNIQUE(zone_id, timestamp)
            )
        ''')
        c.execute("PRAGMA table_info(sensor_readings)")
        columns = [row[1] for row in c.fetchall()]
        if 'temp' not in columns:
            c.execute('ALTER TABLE sensor_readings ADD COLUMN temp REAL')
        if 'humidity' not in columns:
            c.execute('ALTER TABLE sensor_readings ADD COLUMN humidity REAL')
            
        c.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings_15m (
                zone_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                pm2_5 REAL,
                pm10 REAL,
                temp REAL,
                humidity REAL,
                UNIQUE(zone_id, ts)
            )
        ''')

        c.execute('''
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
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS node_offsets (
                zone_id TEXT NOT NULL,
                node_name TEXT NOT NULL,
                metric TEXT NOT NULL,
                factor REAL,
                samples INTEGER,
                updated_at REAL,
                UNIQUE(zone_id, node_name, metric)
            )
        ''')

    c.execute('CREATE INDEX IF NOT EXISTS idx_zone_time ON sensor_readings (zone_id, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_zone_time_15m ON sensor_readings_15m (zone_id, ts)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_node_offsets ON node_offsets (zone_id)')
    conn.commit()
    conn.close()

def save_reading(zone_id, pm25, pm10, temp=None, humidity=None, timestamp=None):
    if timestamp is None:
        timestamp = time.time()
        
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    try:
        if is_pg:
            c.execute('''
                INSERT INTO sensor_readings (zone_id, timestamp, pm2_5, pm10, temp, humidity)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (zone_id, timestamp) DO NOTHING
            ''', (zone_id, timestamp, pm25, pm10, temp, humidity))
        else:
            c.execute('''
                INSERT OR IGNORE INTO sensor_readings (zone_id, timestamp, pm2_5, pm10, temp, humidity)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (zone_id, timestamp, pm25, pm10, temp, humidity))
            
        conn.commit()
    except Exception as e:
        print(f"DB Save Error: {e}")
    finally:
        conn.close()

def save_readings(readings: list[dict]):
    """Batch save multiple readings in a single transaction."""
    if not readings:
        return

    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    try:
        if is_pg:
            c.executemany('''
                INSERT INTO sensor_readings (zone_id, timestamp, pm2_5, pm10, temp, humidity)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (zone_id, timestamp) DO NOTHING
            ''', [
                (r["zone_id"], r["timestamp"], r["pm2_5"], r["pm10"], r.get("temp"), r.get("humidity"))
                for r in readings
            ])
        else:
            c.executemany('''
                INSERT OR IGNORE INTO sensor_readings (zone_id, timestamp, pm2_5, pm10, temp, humidity)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', [
                (r["zone_id"], r["timestamp"], r["pm2_5"], r["pm10"], r.get("temp"), r.get("humidity"))
                for r in readings
            ])
            
        conn.commit()
    except Exception as e:
        print(f"DB Batch Save Error: {e}")
    finally:
        conn.close()

def get_history(zone_id, hours=24):
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')
    
    cutoff = time.time() - (hours * 3600)
    
    query = '''
        SELECT timestamp as ts, pm2_5, pm10, temp, humidity
        FROM sensor_readings 
        WHERE zone_id = %s AND timestamp > %s
        ORDER BY timestamp ASC
    ''' if is_pg else '''
        SELECT timestamp as ts, pm2_5, pm10, temp, humidity
        FROM sensor_readings 
        WHERE zone_id = ? AND timestamp > ?
        ORDER BY timestamp ASC
    '''
    
    c.execute(query, (zone_id, cutoff))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def save_seasonal_climatology(zone_id, months: list[dict]):
    if not months:
        return

    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    try:
        if is_pg:
            c.executemany('''
                INSERT INTO seasonal_climatology (zone_id, month, pm2_5, pm10, precipitation, temp, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (zone_id, month) DO UPDATE SET
                    pm2_5 = EXCLUDED.pm2_5,
                    pm10 = EXCLUDED.pm10,
                    precipitation = EXCLUDED.precipitation,
                    temp = EXCLUDED.temp,
                    updated_at = EXCLUDED.updated_at
            ''', [
                (zone_id, m["month"], m["pm2_5"], m["pm10"], m["precipitation"], m["temp"], m["updated_at"])
                for m in months
            ])
        else:
            c.executemany('''
                INSERT INTO seasonal_climatology (zone_id, month, pm2_5, pm10, precipitation, temp, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(zone_id, month) DO UPDATE SET
                    pm2_5 = excluded.pm2_5,
                    pm10 = excluded.pm10,
                    precipitation = excluded.precipitation,
                    temp = excluded.temp,
                    updated_at = excluded.updated_at
            ''', [
                (zone_id, m["month"], m["pm2_5"], m["pm10"], m["precipitation"], m["temp"], m["updated_at"])
                for m in months
            ])

        conn.commit()
    except Exception as e:
        print(f"DB Climatology Save Error: {e}")
    finally:
        conn.close()

def get_seasonal_climatology(zone_id):
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    query = '''
        SELECT month, pm2_5, pm10, precipitation, temp, updated_at
        FROM seasonal_climatology
        WHERE zone_id = %s
        ORDER BY month ASC
    ''' if is_pg else '''
        SELECT month, pm2_5, pm10, precipitation, temp, updated_at
        FROM seasonal_climatology
        WHERE zone_id = ?
        ORDER BY month ASC
    '''

    c.execute(query, (zone_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_monthly_sensor_averages(zone_id):
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    if is_pg:
        query = '''
            SELECT CAST(EXTRACT(MONTH FROM to_timestamp(timestamp)) AS INTEGER) as month,
                   AVG(pm2_5) as pm2_5, AVG(pm10) as pm10, COUNT(*) as samples
            FROM sensor_readings
            WHERE zone_id = %s
            GROUP BY 1
        '''
    else:
        query = '''
            SELECT CAST(strftime('%m', timestamp, 'unixepoch') AS INTEGER) as month,
                   AVG(pm2_5) as pm2_5, AVG(pm10) as pm10, COUNT(*) as samples
            FROM sensor_readings
            WHERE zone_id = ?
            GROUP BY 1
        '''

    c.execute(query, (zone_id,))
    rows = c.fetchall()
    conn.close()
    return {row["month"]: dict(row) for row in rows}

def refresh_15m_rollups():
    """
    Refresh the 15-minute rollup table (continuous aggregates).
    Call this from a cron job or background task periodically.
    """
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    try:
        if is_pg:
            c.execute('''
                INSERT INTO sensor_readings_15m (zone_id, ts, pm2_5, pm10, temp, humidity)
                SELECT 
                    zone_id, 
                    CAST(timestamp / 900 AS INTEGER) * 900 as ts,
                    AVG(pm2_5), AVG(pm10), AVG(temp), AVG(humidity)
                FROM sensor_readings
                GROUP BY zone_id, CAST(timestamp / 900 AS INTEGER) * 900
                ON CONFLICT (zone_id, ts) DO UPDATE SET 
                    pm2_5 = EXCLUDED.pm2_5,
                    pm10 = EXCLUDED.pm10,
                    temp = EXCLUDED.temp,
                    humidity = EXCLUDED.humidity
            ''')
        else:
            c.execute('''
                INSERT INTO sensor_readings_15m (zone_id, ts, pm2_5, pm10, temp, humidity)
                SELECT 
                    zone_id, 
                    CAST(timestamp / 900 AS INTEGER) * 900 as ts,
                    AVG(pm2_5), AVG(pm10), AVG(temp), AVG(humidity)
                FROM sensor_readings
                GROUP BY zone_id, CAST(timestamp / 900 AS INTEGER) * 900
                ON CONFLICT(zone_id, ts) DO UPDATE SET 
                    pm2_5 = excluded.pm2_5,
                    pm10 = excluded.pm10,
                    temp = excluded.temp,
                    humidity = excluded.humidity
            ''')
        conn.commit()
    except Exception as e:
        print(f"DB Rollup Error: {e}")
    finally:
        conn.close()

def zone_node_names(zone_id: str) -> list:
    """Names of the enabled sensors configured for a zone, empty if it has none."""
    entry = NODES_CONFIG.get(zone_id)
    if not entry:
        return []
    names = []
    for node in entry.get("nodes", []):
        if node.get("enabled", True):
            names.append(node["name"])
    return names


def _fetch_node_columns(zone_id: str, node_names: list, days: int) -> dict:
    """
    Pull recent per sensor readings for a zone, keyed by metric then sensor then bucket.

    Values are returned as natural logs. Working in logs keeps a single filthy day
    from dominating the offset estimate, since the model we are fitting is
    multiplicative: one sensor reads a roughly constant percentage above another,
    not a constant number of micrograms above it.
    """
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    cutoff = time.time() - (days * 86400)
    ids = [f"{zone_id}_{name}" for name in node_names]
    placeholder = "%s" if is_pg else "?"
    slots = ", ".join([placeholder] * len(ids))

    query = f'''
        SELECT zone_id, ts, pm2_5, pm10
        FROM sensor_readings_15m
        WHERE ts > {placeholder} AND zone_id IN ({slots})
    '''

    columns = {metric: {name: {} for name in node_names} for metric in HOMOGENISED_METRICS}
    try:
        c.execute(query, [cutoff] + ids)
        for row in c.fetchall():
            data = dict(row)
            name = data["zone_id"][len(zone_id) + 1:]
            if name not in columns[HOMOGENISED_METRICS[0]]:
                continue
            for metric in HOMOGENISED_METRICS:
                value = data.get(metric)
                if value is not None and value > 0:
                    columns[metric][name][int(data["ts"])] = math.log(value)
    except Exception as e:
        print(f"DB Offset Read Error: {e}")
    finally:
        conn.close()

    return columns


def _solve_offsets(column: dict) -> dict:
    """
    Estimate how each sensor sits against the rest of the network for one metric.

    Sensors come and go, so a plain average per sensor is not comparable: they
    covered different stretches of time with different weather. This solves the
    level and the offsets together by alternating least squares:

        1. assume every offset is zero
        2. per bucket, take the level from whichever sensors reported, net of offsets
        3. re-estimate each offset as its mean gap from that level
        4. re-centre so the offsets average out
        5. repeat until nothing moves

    Returns multiplicative factors normalised to average 1.0, so a zone with every
    sensor reporting keeps the value it already had. Empty if fewer than two
    sensors clear OFFSET_MIN_SAMPLES.
    """
    active = {name: series for name, series in column.items() if len(series) >= OFFSET_MIN_SAMPLES}
    if len(active) < 2:
        return {}

    stamps = set()
    for series in active.values():
        stamps.update(series)

    members = {}
    for stamp in stamps:
        members[stamp] = [name for name in active if stamp in active[name]]

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
    """
    Correct the log space factors so they also balance as ordinary averages.

    The offsets are fitted on logs but the zone series is an arithmetic mean, and
    the two do not agree: a noisier sensor has a higher arithmetic mean than a
    quieter one at the same log mean. Left alone that leaks a systematic bias back
    in when a sensor drops out. One rescaling pass over the buckets where every
    sensor reported cut the leftover bias from 1.12% to 0.27% on Jammu.

    Falls back to the input factors when there is not enough overlap to measure.
    """
    names = sorted(factors)
    shared = None
    for name in names:
        stamps = set(column.get(name) or {})
        if shared is None:
            shared = stamps
        else:
            shared = shared & stamps
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
    """
    Correction for buckets predating per sensor storage, where only a zone row exists.

    Per sensor saving was added after zone saving, so the earliest history cannot be
    decomposed. The best available guess is that those rows came from whichever
    sensors were running in the first week we do have sensor data for. This is an
    inference rather than a measurement, and it is the weakest part of the series.
    """
    earliest = None
    for name in factors:
        series = column.get(name) or {}
        if series:
            first = min(series)
            if earliest is None or first < earliest:
                earliest = first
    if earliest is None:
        return 1.0

    window = earliest + (7 * 86400)
    present = []
    for name in factors:
        series = column.get(name) or {}
        if series and min(series) <= window:
            present.append(name)
    if not present:
        return 1.0

    total = 0.0
    for name in present:
        total += factors[name]
    return total / len(present)


def compute_node_offsets(zone_id: str) -> list:
    """
    Work out the per sensor offsets for a zone, ready to persist.

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


def save_node_offsets(rows: list):
    """Upsert computed sensor offsets, replacing any earlier values for the zone."""
    if not rows:
        return

    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    try:
        payload = [
            (r["zone_id"], r["node_name"], r["metric"], r["factor"], r["samples"], r["updated_at"])
            for r in rows
        ]
        if is_pg:
            c.executemany('''
                INSERT INTO node_offsets (zone_id, node_name, metric, factor, samples, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (zone_id, node_name, metric) DO UPDATE SET
                    factor = EXCLUDED.factor,
                    samples = EXCLUDED.samples,
                    updated_at = EXCLUDED.updated_at
            ''', payload)
        else:
            c.executemany('''
                INSERT INTO node_offsets (zone_id, node_name, metric, factor, samples, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(zone_id, node_name, metric) DO UPDATE SET
                    factor = excluded.factor,
                    samples = excluded.samples,
                    updated_at = excluded.updated_at
            ''', payload)

        conn.commit()
    except Exception as e:
        print(f"DB Offset Save Error: {e}")
    finally:
        conn.close()


def get_node_offsets(zone_id: str) -> dict:
    """Stored offsets for a zone as {metric: {sensor_name: factor}}, empty if never computed."""
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    query = '''
        SELECT node_name, metric, factor
        FROM node_offsets
        WHERE zone_id = %s
    ''' if is_pg else '''
        SELECT node_name, metric, factor
        FROM node_offsets
        WHERE zone_id = ?
    '''

    result = {}
    try:
        c.execute(query, (zone_id,))
        for row in c.fetchall():
            data = dict(row)
            factor = data.get("factor")
            if factor and factor > 0:
                result.setdefault(data["metric"], {})[data["node_name"]] = factor
    except Exception as e:
        print(f"DB Offset Lookup Error: {e}")
    finally:
        conn.close()

    return result


def refresh_stale_node_offsets():
    """
    Recompute sensor offsets for any zone whose stored values are older than a day.

    Offsets move slowly, so this is cheap to skip. It does need to run after a new
    sensor is installed, otherwise that sensor is averaged in uncorrected and
    reintroduces exactly the step this is here to remove.
    """
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    fresh = set()
    try:
        c.execute("SELECT zone_id, MAX(updated_at) as updated_at FROM node_offsets GROUP BY zone_id")
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


def _stream_homogenised(location, node_names, offsets, cutoff, interval_sec, selected_metrics, table, time_col):
    """
    Stream a zone series rebuilt from its sensors, with per sensor offsets divided out.

    For each bucket the value is the mean over the sensors that actually reported,
    of reading / factor. Dividing by the factor restores a sensor to network scale,
    so the series does not step when one joins or drops out. Buckets with no sensor
    rows at all fall back to the stored zone row scaled by the legacy factor.

    Rows arrive ordered by bucket, so this accumulates and flushes one bucket at a
    time and never holds the full series in memory.
    """
    conn = get_connection()
    is_pg = hasattr(conn, 'dsn')

    try:
        if is_pg:
            c = conn.cursor(name='homogenised_data_cursor')
        else:
            c = conn.cursor()

        placeholder = "%s" if is_pg else "?"
        ids = [location] + [f"{location}_{name}" for name in node_names]
        slots = ", ".join([placeholder] * len(ids))
        metrics_sql = ", ".join([f"AVG({m}) as {m}" for m in selected_metrics])
        ts_expr = f"CAST({time_col} / {interval_sec} AS INTEGER) * {interval_sec}"

        query = f'''
            SELECT
                zone_id,
                {ts_expr} as ts,
                {metrics_sql}
            FROM {table}
            WHERE {time_col} > {placeholder} AND zone_id IN ({slots})
            GROUP BY zone_id, 2
            ORDER BY 2 ASC, zone_id ASC
        '''

        c.execute(query, [cutoff] + ids)

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
            rows = c.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                data = dict(row)
                bucket = int(data["ts"])
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

        if current_ts is not None:
            yield flush()

    except Exception as e:
        print(f"DB Homogenised Stream Error: {e}")
    finally:
        try:
            c.close()
        except:
            pass
        if is_pg:
            try:
                conn.rollback()
            except:
                pass
        try:
            conn.close()
        except:
            pass


def stream_historical_data(location: str, time_range_sec: int, interval_sec: int, metrics: list):
    """
    Streams historical data from the database, grouping by the specified interval.
    Uses server-side cursors in PostgreSQL to prevent memory overload.

    For multi sensor zones the series is rebuilt from the individual sensor rows with
    per sensor offsets applied, so that a sensor joining or dropping out of the zone
    does not show up as a change in air quality.
    """
    valid_metrics = {'pm2.5': 'pm2_5', 'pm10': 'pm10', 'temp': 'temp', 'humidity': 'humidity'}
    selected_metrics = [valid_metrics[m] for m in metrics if m in valid_metrics]
    if not selected_metrics:
        selected_metrics = ['pm2_5', 'pm10']

    if interval_sec >= 900 and interval_sec % 900 == 0:
        table = "sensor_readings_15m"
        time_col = "ts"
    else:
        table = "sensor_readings"
        time_col = "timestamp"

    cutoff = time.time() - time_range_sec

    if location != "all":
        node_names = zone_node_names(location)
        if len(node_names) >= 2:
            offsets = get_node_offsets(location)
            if offsets:
                yield from _stream_homogenised(
                    location, node_names, offsets, cutoff,
                    interval_sec, selected_metrics, table, time_col)
                return

    conn = get_connection()
    is_pg = hasattr(conn, 'dsn')
    
    try:
        if is_pg:
            # Server-side cursor for PostgreSQL to stream results
            c = conn.cursor(name='historical_data_cursor')
        else:
            c = conn.cursor()

        cutoff = time.time() - time_range_sec
        
        valid_metrics = {'pm2.5': 'pm2_5', 'pm10': 'pm10', 'temp': 'temp', 'humidity': 'humidity'}
        selected_metrics = [valid_metrics[m] for m in metrics if m in valid_metrics]
        if not selected_metrics:
            selected_metrics = ['pm2_5', 'pm10']
            
        metrics_sql = ", ".join([f"AVG({m}) as {m}" for m in selected_metrics])
        
        # Use rollup table if interval is a multiple of 15m
        if interval_sec >= 900 and interval_sec % 900 == 0:
            table = "sensor_readings_15m"
            time_col = "ts"
        else:
            table = "sensor_readings"
            time_col = "timestamp"
            
        # grouping by interval. 
        ts_expr = f"CAST({time_col} / {interval_sec} AS INTEGER) * {interval_sec}"
        
        where_clause = f"{time_col} > %s" if is_pg else f"{time_col} > ?"
        params = [cutoff]
        
        if location != "all":
            where_clause += " AND zone_id = %s" if is_pg else " AND zone_id = ?"
            params.append(location)
            
        query = f'''
            SELECT 
                zone_id, 
                {ts_expr} as ts,
                {metrics_sql}
            FROM {table}
            WHERE {where_clause}
            GROUP BY zone_id, 2
            ORDER BY 2 ASC, zone_id ASC
        '''
        
        c.execute(query, params)
        
        while True:
            rows = c.fetchmany(1000)
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
        try:
            c.close()
        except:
            pass
        if is_pg:
            try:
                conn.rollback()
            except:
                pass
        try:
            conn.close()
        except:
            pass

init_db()
