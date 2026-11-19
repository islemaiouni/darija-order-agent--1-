import os
import sqlite3
import uuid
import logging
from contextlib import contextmanager
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("db")

DB_PATH = os.getenv("DATABASE_PATH", "./agent.db")
FINAL_STATUSES = {"CONFIRMED", "REJECTED", "NEEDS_HUMAN_REVIEW", "NO_ANSWER", "CALL_FAILED"}


@contextmanager
def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            total_dzd INTEGER NOT NULL,
            delivery_address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
            confirmed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS calls (
            call_sid TEXT PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES orders(id),
            call_status TEXT NOT NULL DEFAULT 'INITIATED',
            turn_count INTEGER NOT NULL DEFAULT 0,
            last_transcript TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS call_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT NOT NULL REFERENCES calls(call_sid),
            turn_number INTEGER NOT NULL,
            transcript TEXT,
            intent TEXT,
            agent_response TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(call_sid, turn_number)
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(phone);
        """)


# ---------- orders ----------
def create_order(name: str, phone: str, product_name: str, quantity: int,
                 total_dzd: int, address: str) -> str:
    order_id = "DZ-" + uuid.uuid4().hex[:12].upper()
    with connect() as db:
        db.execute(
            """INSERT INTO orders
               (id, customer_name, phone, product_name, quantity, total_dzd, delivery_address)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_id, name, phone, product_name, quantity, total_dzd, address),
        )
    mirror_order(order_id)
    return order_id


def get_order(order_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None


def recent_order_for_phone(phone: str, minutes: int) -> bool:
    """True if this phone already placed an order in the last N minutes (anti-abuse)."""
    with connect() as db:
        row = db.execute(
            "SELECT 1 FROM orders WHERE phone=? AND created_at > datetime('now', ?) LIMIT 1",
            (phone, f"-{int(minutes)} minutes"),
        ).fetchone()
        return row is not None


def create_demo_order(phone: str = "+213XXXXXXXXX"):
    with connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO orders
               (id, customer_name, phone, product_name, quantity, total_dzd, delivery_address)
               VALUES ('DZ-DEMO-001', 'Mohamed', ?, 'Samsung Galaxy A55', 1, 45000, 'Annaba, Algeria')""",
            (phone,),
        )
    mirror_order("DZ-DEMO-001")


# ---------- calls ----------
def create_call(call_sid: str, order_id: str):
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO calls(call_sid, order_id) VALUES (?, ?)",
                   (call_sid, order_id))


def get_call(call_sid: str):
    with connect() as db:
        row = db.execute("SELECT * FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
        return dict(row) if row else None


def get_latest_call(order_id: str):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM calls WHERE order_id=? ORDER BY created_at DESC LIMIT 1", (order_id,)
        ).fetchone()
        return dict(row) if row else None


def get_turns(order_id: str):
    with connect() as db:
        rows = db.execute(
            """SELECT t.turn_number, t.transcript, t.intent, t.agent_response
               FROM call_turns t JOIN calls c ON c.call_sid = t.call_sid
               WHERE c.order_id=? ORDER BY t.id""",
            (order_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_turn(call_sid: str, transcript: str, intent: str, response: str) -> int:
    with connect() as db:
        row = db.execute("SELECT turn_count FROM calls WHERE call_sid=?", (call_sid,)).fetchone()
        if not row:
            raise ValueError("Unknown call session")
        turn = row["turn_count"] + 1
        db.execute(
            """INSERT INTO call_turns(call_sid, turn_number, transcript, intent, agent_response)
               VALUES (?, ?, ?, ?, ?)""",
            (call_sid, turn, transcript, intent, response),
        )
        db.execute(
            """UPDATE calls SET turn_count=?, last_transcript=?, updated_at=CURRENT_TIMESTAMP
               WHERE call_sid=?""",
            (turn, transcript, call_sid),
        )
        return turn


def set_call_status(call_sid: str, status: str):
    with connect() as db:
        db.execute(
            """UPDATE calls SET call_status=?, updated_at=CURRENT_TIMESTAMP WHERE call_sid=?
               AND call_status NOT IN ('CONFIRMED','REJECTED','NEEDS_HUMAN_REVIEW','NO_ANSWER','CALL_FAILED')""",
            (status, call_sid),
        )


def finalize_order(call_sid: str, order_id: str, status: str):
    """Only Python code (never the LLM) calls this. Only PENDING orders can change."""
    if status not in FINAL_STATUSES:
        raise ValueError("Invalid final status")
    with connect() as db:
        db.execute(
            """UPDATE orders SET status=?,
               confirmed_at=CASE WHEN ?='CONFIRMED' THEN CURRENT_TIMESTAMP ELSE confirmed_at END
               WHERE id=? AND status='PENDING_CONFIRMATION'""",
            (status, status, order_id),
        )
        if call_sid:
            db.execute(
                """UPDATE calls SET call_status=?, updated_at=CURRENT_TIMESTAMP WHERE call_sid=?
                   AND call_status NOT IN ('CONFIRMED','REJECTED','NEEDS_HUMAN_REVIEW','NO_ANSWER','CALL_FAILED')""",
                (status, call_sid),
            )
    mirror_order(order_id)


# ---------- optional Supabase mirror ----------
def mirror_order(order_id: str):
    """If SUPABASE_URL + SUPABASE_SERVICE_KEY are set, upsert the order into Supabase.
    SQLite stays the source of truth; failures here never break a call."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    order = get_order(order_id)
    if not order:
        return
    try:
        r = requests.post(
            f"{url}/rest/v1/orders?on_conflict=id",
            json=order,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            timeout=10,
        )
        if r.status_code >= 300:
            log.warning("Supabase mirror failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("Supabase mirror error: %s", e)
