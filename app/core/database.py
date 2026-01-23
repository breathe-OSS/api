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
                UNIQUE(zone_id, timestamp)
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
                UNIQUE(zone_id, timestamp)
            )
        ''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_zone_time ON sensor_readings (zone_id, timestamp)')
    conn.commit()
    conn.close()

def save_reading(zone_id, pm25, pm10, timestamp=None):
    if timestamp is None:
        timestamp = time.time()
        
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')

    try:
        if is_pg:
            c.execute('''
                INSERT INTO sensor_readings (zone_id, timestamp, pm2_5, pm10)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (zone_id, timestamp) DO NOTHING
            ''', (zone_id, timestamp, pm25, pm10))
        else:
            c.execute('''
                INSERT OR IGNORE INTO sensor_readings (zone_id, timestamp, pm2_5, pm10)
                VALUES (?, ?, ?, ?)
            ''', (zone_id, timestamp, pm25, pm10))
            
        conn.commit()
    except Exception as e:
        print(f"DB Save Error: {e}")
    finally:
        conn.close()

def get_history(zone_id, hours=24):
    conn = get_connection()
    c = conn.cursor()
    is_pg = hasattr(conn, 'dsn')
    
    cutoff = time.time() - (hours * 3600)
    
    query = '''
        SELECT timestamp as ts, pm2_5, pm10 
        FROM sensor_readings 
        WHERE zone_id = %s AND timestamp > %s
        ORDER BY timestamp ASC
    ''' if is_pg else '''
        SELECT timestamp as ts, pm2_5, pm10 
        FROM sensor_readings 
        WHERE zone_id = ? AND timestamp > ?
        ORDER BY timestamp ASC
    '''
    
    c.execute(query, (zone_id, cutoff))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

init_db()
