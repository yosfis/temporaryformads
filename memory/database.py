from __future__ import annotations
import json, sqlite3, uuid
from contextlib import contextmanager
from typing import Any, Iterator
from config import DB_PATH, STAGES
from memory.schemas import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS ventures (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, idea TEXT NOT NULL,
  theme TEXT NOT NULL DEFAULT 'indigo',
  current_stage TEXT NOT NULL DEFAULT 'discover',
  brief_json TEXT NOT NULL DEFAULT '{}',
  gate TEXT NOT NULL DEFAULT 'pending',
  gate_rationale TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stage_state (
  venture_id TEXT NOT NULL, stage TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'not_started',
  notes_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
  PRIMARY KEY (venture_id, stage));
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, venture_id TEXT NOT NULL, type TEXT NOT NULL,
  claim TEXT NOT NULL, source TEXT, details TEXT, agent TEXT, stage TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, venture_id TEXT NOT NULL, stage TEXT, agent TEXT,
  action TEXT NOT NULL, decision TEXT NOT NULL, rationale TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  approval_status TEXT NOT NULL DEFAULT 'not_required',
  model TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, venture_id TEXT NOT NULL, action TEXT NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, venture_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
"""

@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        # Idempotent migration: add model column if missing
        try:
            c.execute("ALTER TABLE decisions ADD COLUMN model TEXT")
        except sqlite3.OperationalError:
            pass
        # Idempotent migration: structured founder-interview state
        try:
            c.execute("ALTER TABLE ventures ADD COLUMN discovery_json TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass

def create_venture(name, idea, theme="indigo"):
    vid = str(uuid.uuid4()); now = utcnow()
    with connect() as c:
        c.execute(
            "INSERT INTO ventures (id,name,idea,theme,current_stage,brief_json,"
            "gate,gate_rationale,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vid, name, idea, theme, "discover", "{}", "pending", "", now, now))
        for stage in STAGES:
            c.execute(
                "INSERT INTO stage_state (venture_id,stage,status,notes_json,updated_at) "
                "VALUES (?,?,?,?,?)", (vid, stage, "not_started", "{}", now))
    return get_venture(vid)

def get_venture(vid):
    with connect() as c:
        row = c.execute("SELECT * FROM ventures WHERE id=?", (vid,)).fetchone()
    if not row:
        return None
    v = dict(row)
    v["brief"] = json.loads(v.pop("brief_json") or "{}")
    v["discovery"] = json.loads(v.pop("discovery_json", None) or "{}")
    return v

def list_ventures():
    with connect() as c:
        rows = c.execute(
            "SELECT id,name,idea,theme,current_stage,gate,created_at FROM ventures "
            "ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def update_venture(vid, **fields):
    if not fields:
        return
    if "brief" in fields:
        fields["brief_json"] = json.dumps(fields.pop("brief"))
    if "discovery" in fields:
        fields["discovery_json"] = json.dumps(fields.pop("discovery"))
    fields["updated_at"] = utcnow()
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE ventures SET {sets} WHERE id=?",
                  (*fields.values(), vid))

def get_stages(vid):
    with connect() as c:
        rows = c.execute("SELECT * FROM stage_state WHERE venture_id=?", (vid,)).fetchall()
    return {r["stage"]: {**dict(r), "notes": json.loads(r["notes_json"] or "{}")}
            for r in rows}

def set_stage_status(vid, stage, status):
    with connect() as c:
        c.execute("UPDATE stage_state SET status=?, updated_at=? "
                  "WHERE venture_id=? AND stage=?",
                  (status, utcnow(), vid, stage))

def add_message(vid, role, content):
    with connect() as c:
        c.execute("INSERT INTO messages (venture_id,role,content,created_at) "
                  "VALUES (?,?,?,?)", (vid, role, content, utcnow()))

def get_messages(vid, limit=200):
    with connect() as c:
        rows = c.execute(
            "SELECT role,content,created_at FROM messages WHERE venture_id=? "
            "ORDER BY id ASC LIMIT ?", (vid, limit)).fetchall()
    return [dict(r) for r in rows]

init_db()
