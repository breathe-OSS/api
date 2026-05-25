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
import psycopg2
from psycopg2.extras import RealDictCursor

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "breathe.db")

def get_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
        return conn
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

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
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_zone_time ON sensor_readings (zone_id, timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_zone_time_15m ON sensor_readings_15m (zone_id, ts)')
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

def stream_historical_data(location: str, time_range_sec: int, interval_sec: int, metrics: list):
    """
    Streams historical data from the database, grouping by the specified interval.
    Uses server-side cursors in PostgreSQL to prevent memory overload.
    """
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
