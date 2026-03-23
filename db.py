"""db.py — SQLite data layer for Outreach.

Every data table maps 1-to-1 to a former CSV / text file:
  leads         ← leads.csv         (staging / recent scrape)
  live_leads    ← live_leads.csv    (permanent cumulative store)
  sent_log      ← sent_log.csv
  replies_log   ← replies_log.csv
  pipeline      ← pipeline.csv
  unsubscribe   ← unsubscribe.txt

Usage:
  import db
  db.init_db()                        # call once on startup
  df = db.read_table("live_leads")
  db.write_table("leads", my_df)
  db.append_rows("sent_log", [row])
"""

import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Set

import pandas as pd

# ─── Config ────────────────────────────────────────────────────────────────
DB_PATH = "outreach.db"

# Maps CSV filename → table name (for transparent routing)
CSV_TO_TABLE: Dict[str, str] = {
    "leads.csv":        "leads",
    "live_leads.csv":   "live_leads",
    "sent_log.csv":     "sent_log",
    "replies_log.csv":  "replies_log",
    "pipeline.csv":     "pipeline",
}

# Known columns used when returning empty DataFrames
_EMPTY_COLS: Dict[str, List[str]] = {
    "pipeline": [
        "email", "name", "status", "deal_value",
        "reply_tag", "source", "keyword", "niche", "last_updated",
    ],
    "sent_log": [
        "timestamp", "to_email", "to_name", "subject",
        "status", "error", "sequence_num",
    ],
    "replies_log": [
        "logged_at", "from_email", "name", "subject", "date", "reply_tag",
    ],
    "unsubscribe": ["email"],
}

_lock = threading.Lock()

# ─── Schema ────────────────────────────────────────────────────────────────
_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT DEFAULT '',
    keyword         TEXT DEFAULT '',
    niche           TEXT DEFAULT '',
    city            TEXT DEFAULT '',
    url             TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    whatsapp_number TEXT DEFAULT '',
    has_whatsapp    TEXT DEFAULT '',
    website         TEXT DEFAULT '',
    linkedin        TEXT DEFAULT '',
    twitter         TEXT DEFAULT '',
    facebook        TEXT DEFAULT '',
    instagram       TEXT DEFAULT '',
    contact_page    TEXT DEFAULT '',
    issues          TEXT DEFAULT '',
    lead_score      TEXT DEFAULT '',
    rating          TEXT DEFAULT '',
    reviews         TEXT DEFAULT '',
    category        TEXT DEFAULT '',
    address         TEXT DEFAULT '',
    notes           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS live_leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT DEFAULT '',
    keyword         TEXT DEFAULT '',
    niche           TEXT DEFAULT '',
    city            TEXT DEFAULT '',
    url             TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    whatsapp_number TEXT DEFAULT '',
    has_whatsapp    TEXT DEFAULT '',
    website         TEXT DEFAULT '',
    linkedin        TEXT DEFAULT '',
    twitter         TEXT DEFAULT '',
    facebook        TEXT DEFAULT '',
    instagram       TEXT DEFAULT '',
    contact_page    TEXT DEFAULT '',
    issues          TEXT DEFAULT '',
    lead_score      TEXT DEFAULT '',
    rating          TEXT DEFAULT '',
    reviews         TEXT DEFAULT '',
    category        TEXT DEFAULT '',
    address         TEXT DEFAULT '',
    notes           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sent_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT DEFAULT '',
    to_email     TEXT DEFAULT '',
    to_name      TEXT DEFAULT '',
    subject      TEXT DEFAULT '',
    status       TEXT DEFAULT '',
    error        TEXT DEFAULT '',
    sequence_num INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pipeline (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT UNIQUE DEFAULT '',
    name         TEXT DEFAULT '',
    status       TEXT DEFAULT 'new',
    deal_value   TEXT DEFAULT '0',
    reply_tag    TEXT DEFAULT '',
    source       TEXT DEFAULT '',
    keyword      TEXT DEFAULT '',
    niche        TEXT DEFAULT '',
    last_updated TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS replies_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at  TEXT DEFAULT '',
    from_email TEXT DEFAULT '',
    name       TEXT DEFAULT '',
    subject    TEXT DEFAULT '',
    date       TEXT DEFAULT '',
    reply_tag  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS unsubscribe (
    email TEXT PRIMARY KEY
);
"""


# ─── Connection ────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─── Initialisation ────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't already exist. Safe to call on every startup."""
    with _lock:
        conn = _get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()


# ─── Core read / write ─────────────────────────────────────────────────────

def read_table(table: str) -> pd.DataFrame:
    """Return entire table as a DataFrame (id column stripped)."""
    conn = _get_conn()
    try:
        df = pd.read_sql(f"SELECT * FROM [{table}]", conn)
    except Exception:
        return pd.DataFrame(columns=_EMPTY_COLS.get(table, []))
    finally:
        conn.close()
    if "id" in df.columns:
        df = df.drop(columns=["id"])
    return df.fillna("")


def write_table(table: str, df: pd.DataFrame) -> None:
    """Replace entire table with the contents of df."""
    with _lock:
        write_df = df.copy()
        if "id" in write_df.columns:
            write_df = write_df.drop(columns=["id"])
        conn = _get_conn()
        write_df.to_sql(table, conn, if_exists="replace", index=False)
        conn.commit()
        conn.close()


def append_rows(table: str, rows: List[Dict]) -> None:
    """Append a list of row dicts to a table."""
    if not rows:
        return
    df = pd.DataFrame(rows)
    with _lock:
        conn = _get_conn()
        df.to_sql(table, conn, if_exists="append", index=False)
        conn.commit()
        conn.close()


def append_sent_log_row(record: Dict) -> None:
    """Thread-safe append of a single sent_log record."""
    append_rows("sent_log", [record])


# ─── Transparent CSV routing ──────────────────────────────────────────────

def read_csv_as_table(csv_path: str) -> pd.DataFrame:
    """
    Drop-in replacement for pd.read_csv — routes known CSV paths to SQLite.
    Unknown files fall back to reading the actual file.
    """
    table = CSV_TO_TABLE.get(Path(csv_path).name)
    if table:
        return read_table(table)
    p = Path(csv_path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str).fillna("")


def write_csv_as_table(csv_path: str, df: pd.DataFrame) -> None:
    """
    Drop-in replacement for df.to_csv — routes known CSV paths to SQLite.
    Unknown files fall back to writing the actual file.
    """
    table = CSV_TO_TABLE.get(Path(csv_path).name)
    if table:
        write_table(table, df)
    else:
        df.to_csv(csv_path, index=False)


# ─── Unsubscribe helpers ───────────────────────────────────────────────────

def load_unsubscribe() -> List[str]:
    """Return sorted list of unsubscribed emails."""
    conn = _get_conn()
    rows = conn.execute("SELECT email FROM unsubscribe ORDER BY email").fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_unsubscribe(emails: List[str]) -> None:
    """Replace the unsubscribe list with the given emails."""
    cleaned = sorted({e.lower().strip() for e in emails if e.strip()})
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM unsubscribe")
        conn.executemany(
            "INSERT OR IGNORE INTO unsubscribe (email) VALUES (?)",
            [(e,) for e in cleaned],
        )
        conn.commit()
        conn.close()


def add_unsubscribe(email: str) -> bool:
    """Add a single email. Returns True if newly added, False if already present."""
    email = email.lower().strip()
    if not email:
        return False
    with _lock:
        conn = _get_conn()
        before = conn.execute(
            "SELECT COUNT(*) FROM unsubscribe WHERE email=?", (email,)
        ).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO unsubscribe (email) VALUES (?)", (email,))
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM unsubscribe WHERE email=?", (email,)
        ).fetchone()[0]
        conn.close()
    return after > before


def get_unsubscribe_set() -> Set[str]:
    """Return set of all unsubscribed emails (for fast membership tests)."""
    conn = _get_conn()
    rows = conn.execute("SELECT email FROM unsubscribe").fetchall()
    conn.close()
    return {r[0] for r in rows}


# ─── One-time CSV migration ────────────────────────────────────────────────

def migrate_csv_if_exists(csv_path: str) -> int:
    """
    Import an existing CSV file into its corresponding SQLite table.
    Skips if the file doesn't exist or the table already has rows.
    Returns the number of rows imported (0 = skipped).
    """
    p = Path(csv_path)
    if not p.exists() or p.stat().st_size == 0:
        return 0
    table = CSV_TO_TABLE.get(p.name)
    if not table:
        return 0
    # Only migrate if table is currently empty
    conn = _get_conn()
    count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
    conn.close()
    if count > 0:
        return 0
    df = pd.read_csv(p, dtype=str).fillna("")
    write_table(table, df)
    return len(df)


def migrate_unsubscribe_txt_if_exists(txt_path: str) -> int:
    """
    Import unsubscribe.txt into the unsubscribe table.
    Skips if file doesn't exist or table already has rows.
    Returns number of emails imported.
    """
    p = Path(txt_path)
    if not p.exists():
        return 0
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM unsubscribe").fetchone()[0]
    conn.close()
    if count > 0:
        return 0
    emails = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            emails.append(line)
    if not emails:
        return 0
    save_unsubscribe(emails)
    return len(emails)
