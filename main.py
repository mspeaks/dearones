from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic
from fastapi import APIRouter, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "dear_son.db")))
APP_PASSWORD = os.getenv("APP_PASSWORD", "lexbase")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY", "")
AGENTMAIL_FROM_INBOX = os.getenv("AGENTMAIL_FROM_INBOX", os.getenv("AGENTMAIL_INBOX", "lex@agentmail.to"))
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "")
SEED_DEMO = os.getenv("DEAR_ONES_SEED_DEMO", "0") == "1"

COOKIE_NAME = "dear_ones_session"
COOKIE_PATH = "/"
SESSIONS: set[str] = set()

RELATIONSHIP_TYPES = {
    "child",
    "parent",
    "partner",
    "sibling",
    "grandparent",
    "friend",
    "other",
}
LETTER_FREQUENCIES = {"monthly", "quarterly", "yearly"}
DEFAULT_FREQUENCY_BY_RELATIONSHIP = {
    "child": "monthly",
    "partner": "quarterly",
    "parent": "quarterly",
    "sibling": "yearly",
    "grandparent": "quarterly",
    "friend": "yearly",
    "other": "yearly",
}
MILESTONE_LABELS = {
    "birthday",
    "anniversary",
    "graduation",
    "new baby",
    "memorial",
    "custom",
}

app = FastAPI(title="Dear Ones")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/dear-son/static", StaticFiles(directory=str(BASE_DIR / "static")), name="legacy_static")


class LoginIn(BaseModel):
    password: str


class ProfileUpdateIn(BaseModel):
    first_name: str | None = None
    default_signature_name: str | None = None
    timezone: str | None = None
    email: str | None = None
    reminders_enabled: bool | None = None


class PersonIn(BaseModel):
    name: str
    relationship_type: str = "other"
    relationship_label: str | None = None
    email: str | None = None
    letter_frequency: str | None = None
    signature_name: str | None = None
    milestones: list[dict[str, Any]] | None = None


class PersonUpdateIn(BaseModel):
    name: str | None = None
    relationship_type: str | None = None
    relationship_label: str | None = None
    email: str | None = None
    letter_frequency: str | None = None
    signature_name: str | None = None
    archived_at: str | None = None
    milestones: list[dict[str, Any]] | None = None


class MilestoneIn(BaseModel):
    label: str
    date: str
    is_recurring: bool = True


class EntryIn(BaseModel):
    content: str
    entry_type: str = "text"
    tags: str | None = None


class GenerateLetterIn(BaseModel):
    period: str | None = None
    reflection_id: int | None = None


class NewLetterIn(BaseModel):
    title: str | None = None
    content: str | None = None
    origin: str = "manual"


class LetterUpdateIn(BaseModel):
    content: str | None = None
    status: str | None = None
    title: str | None = None


class PolishLetterIn(BaseModel):
    content: str | None = None
    note: str | None = None


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def month_key_for(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_start(d: date) -> date:
    return d.replace(day=1)


def shift_months(d: date, delta_months: int) -> date:
    year = d.year + (d.month - 1 + delta_months) // 12
    month = (d.month - 1 + delta_months) % 12 + 1
    return date(year, month, 1)


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def cadence_months(letter_frequency: str) -> int:
    return {"monthly": 1, "quarterly": 3, "yearly": 12}.get(letter_frequency, 12)


def normalize_relationship(value: str | None) -> str:
    if not value:
        return "other"
    relationship = value.strip().lower()
    if relationship not in RELATIONSHIP_TYPES:
        return "other"
    return relationship


def normalize_letter_frequency(relationship_type: str, value: str | None) -> str:
    if value:
        candidate = value.strip().lower()
        if candidate in LETTER_FREQUENCIES:
            return candidate
    return DEFAULT_FREQUENCY_BY_RELATIONSHIP.get(relationship_type, "yearly")


def dict_rows(cursor: sqlite3.Cursor):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def fk_targets(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    return {row["table"] for row in rows}


def ensure_people_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            relationship_type TEXT NOT NULL DEFAULT 'other',
            relationship_label TEXT,
            email TEXT,
            letter_frequency TEXT NOT NULL DEFAULT 'yearly',
            signature_name TEXT,
            created_at TEXT,
            archived_at TEXT
        )
        """
    )


def ensure_user_profile_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            first_name TEXT NOT NULL DEFAULT 'Mark',
            email TEXT,
            default_signature_name TEXT NOT NULL DEFAULT 'Mark',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            reminders_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    if "email" not in table_columns(conn, "user_profile"):
        conn.execute("ALTER TABLE user_profile ADD COLUMN email TEXT")
    row = conn.execute("SELECT id FROM user_profile WHERE id = 1").fetchone()
    if row:
        return
    now = now_iso()
    conn.execute(
        """
        INSERT INTO user_profile (id, first_name, default_signature_name, timezone, reminders_enabled, created_at, updated_at)
        VALUES (1, 'Mark', 'Mark', 'UTC', 1, ?, ?)
        """,
        (now, now),
    )


def ensure_milestone_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS person_milestones (
            id INTEGER PRIMARY KEY,
            person_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            date TEXT NOT NULL,
            is_recurring INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            FOREIGN KEY(person_id) REFERENCES people(id)
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS person_milestone_unique_idx
        ON person_milestones(person_id, label, date)
        """
    )


def ensure_reflections_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monthly_reflections (
            id INTEGER PRIMARY KEY,
            person_id INTEGER NOT NULL,
            month_key TEXT NOT NULL,
            summary_bullets_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'ready',
            draft_prompted_at TEXT,
            draft_generated_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            FOREIGN KEY(person_id) REFERENCES people(id)
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS monthly_reflections_person_month_idx
        ON monthly_reflections(person_id, month_key)
        """
    )


def ensure_reminder_log_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_digest_log (
            id INTEGER PRIMARY KEY,
            run_key TEXT UNIQUE NOT NULL,
            offset_days INTEGER NOT NULL,
            created_at TEXT
        )
        """
    )


def migrate_children_to_people(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "children"):
        return
    now = now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO people (
            id, name, relationship_type, relationship_label, email, letter_frequency, signature_name, created_at, archived_at
        )
        SELECT id, name, 'child', NULL, email, 'monthly', NULL, COALESCE(created_at, ?), NULL
        FROM children
        """,
        (now,),
    )
    if "dob" in table_columns(conn, "children"):
        conn.execute(
            """
            INSERT OR IGNORE INTO person_milestones (person_id, label, date, is_recurring, created_at)
            SELECT id, 'birthday', dob, 1, COALESCE(created_at, ?)
            FROM children
            WHERE dob IS NOT NULL AND TRIM(dob) != ''
            """,
            (now,),
        )


def create_entries_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            person_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            entry_type TEXT DEFAULT 'text',
            tags TEXT,
            created_at TEXT,
            FOREIGN KEY(person_id) REFERENCES people(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS entries_person_created_idx ON entries(person_id, created_at)")


def rebuild_entries_table(conn: sqlite3.Connection) -> None:
    columns = table_columns(conn, "entries")
    person_expr = "person_id" if "person_id" in columns else "child_id"
    tags_expr = "tags" if "tags" in columns else "NULL"
    created_expr = "COALESCE(created_at, CURRENT_TIMESTAMP)" if "created_at" in columns else "CURRENT_TIMESTAMP"
    entry_type_expr = "COALESCE(entry_type, 'text')" if "entry_type" in columns else "'text'"
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE entries RENAME TO entries_legacy")
        create_entries_table(conn)
        conn.execute(
            f"""
            INSERT INTO entries (id, person_id, content, entry_type, tags, created_at)
            SELECT id, {person_expr}, content, {entry_type_expr}, {tags_expr}, {created_expr}
            FROM entries_legacy
            """
        )
        conn.execute("DROP TABLE entries_legacy")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def ensure_entries_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "entries"):
        create_entries_table(conn)
        return
    columns = table_columns(conn, "entries")
    targets = fk_targets(conn, "entries")
    needs_rebuild = ("person_id" not in columns) or ("people" not in targets)
    if needs_rebuild:
        rebuild_entries_table(conn)
        return
    if "tags" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN tags TEXT")
    if "entry_type" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN entry_type TEXT DEFAULT 'text'")
    if "created_at" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN created_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS entries_person_created_idx ON entries(person_id, created_at)")


def create_letters_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE letters (
            id INTEGER PRIMARY KEY,
            person_id INTEGER NOT NULL,
            title TEXT,
            content TEXT,
            status TEXT DEFAULT 'draft',
            period TEXT,
            source_month_start TEXT,
            source_month_end TEXT,
            origin TEXT DEFAULT 'ai_draft',
            parent_letter_id INTEGER,
            created_at TEXT,
            updated_at TEXT,
            sent_at TEXT,
            FOREIGN KEY(person_id) REFERENCES people(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS letters_person_created_idx ON letters(person_id, created_at)")


def rebuild_letters_table(conn: sqlite3.Connection) -> None:
    columns = table_columns(conn, "letters")
    person_expr = "person_id" if "person_id" in columns else "child_id"
    title_expr = "title" if "title" in columns else "NULL"
    content_expr = "content" if "content" in columns else "NULL"
    status_expr = "COALESCE(status, 'draft')" if "status" in columns else "'draft'"
    period_expr = "period" if "period" in columns else "NULL"
    source_start_expr = (
        "source_month_start"
        if "source_month_start" in columns
        else "CASE WHEN period IS NOT NULL THEN period || '-01' ELSE NULL END"
    )
    source_end_expr = "source_month_end" if "source_month_end" in columns else "NULL"
    origin_expr = "COALESCE(origin, 'ai_draft')" if "origin" in columns else "'ai_draft'"
    parent_expr = "parent_letter_id" if "parent_letter_id" in columns else "NULL"
    created_expr = "COALESCE(created_at, CURRENT_TIMESTAMP)" if "created_at" in columns else "CURRENT_TIMESTAMP"
    updated_expr = "COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)" if "updated_at" in columns else created_expr
    sent_expr = (
        "sent_at"
        if "sent_at" in columns
        else "CASE WHEN status = 'sent' THEN COALESCE(created_at, CURRENT_TIMESTAMP) ELSE NULL END"
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE letters RENAME TO letters_legacy")
        create_letters_table(conn)
        conn.execute(
            f"""
            INSERT INTO letters (
                id, person_id, title, content, status, period, source_month_start, source_month_end,
                origin, parent_letter_id, created_at, updated_at, sent_at
            )
            SELECT
                id, {person_expr}, {title_expr}, {content_expr}, {status_expr}, {period_expr},
                {source_start_expr}, {source_end_expr}, {origin_expr}, {parent_expr},
                {created_expr}, {updated_expr}, {sent_expr}
            FROM letters_legacy
            """
        )
        conn.execute("DROP TABLE letters_legacy")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def ensure_letters_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "letters"):
        create_letters_table(conn)
        return
    columns = table_columns(conn, "letters")
    targets = fk_targets(conn, "letters")
    needs_rebuild = ("person_id" not in columns) or ("people" not in targets)
    if needs_rebuild:
        rebuild_letters_table(conn)
        return
    for col, col_type in [
        ("source_month_start", "TEXT"),
        ("source_month_end", "TEXT"),
        ("origin", "TEXT DEFAULT 'ai_draft'"),
        ("parent_letter_id", "INTEGER"),
        ("updated_at", "TEXT"),
        ("sent_at", "TEXT"),
    ]:
        if col not in columns:
            conn.execute(f"ALTER TABLE letters ADD COLUMN {col} {col_type}")
    conn.execute("CREATE INDEX IF NOT EXISTS letters_person_created_idx ON letters(person_id, created_at)")


def normalize_people_defaults(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, relationship_type, letter_frequency FROM people").fetchall()
    for row in rows:
        relationship = normalize_relationship(row["relationship_type"])
        letter_frequency = normalize_letter_frequency(relationship, row["letter_frequency"])
        conn.execute(
            "UPDATE people SET relationship_type = ?, letter_frequency = ? WHERE id = ?",
            (relationship, letter_frequency, row["id"]),
        )


def init_db() -> None:
    with closing(get_conn()) as conn:
        ensure_people_table(conn)
        ensure_user_profile_table(conn)
        ensure_milestone_table(conn)
        migrate_children_to_people(conn)
        ensure_entries_table(conn)
        ensure_letters_table(conn)
        ensure_reflections_table(conn)
        ensure_reminder_log_table(conn)
        normalize_people_defaults(conn)
        conn.commit()


def seed_data() -> None:
    if not SEED_DEMO:
        return
    with closing(get_conn()) as conn:
        count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        if count > 0:
            return
        created_at = now_iso()
        for name, email, birthday in [
            ("Rowan", "rowanpaulschenker@gmail.com", "2022-09-25"),
            ("Raven", "ravenjoschenker@gmail.com", "2024-11-28"),
        ]:
            cur = conn.execute(
                """
                INSERT INTO people (name, relationship_type, relationship_label, email, letter_frequency, signature_name, created_at, archived_at)
                VALUES (?, 'child', NULL, ?, 'monthly', NULL, ?, NULL)
                """,
                (name, email, created_at),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO person_milestones (person_id, label, date, is_recurring, created_at)
                VALUES (?, 'birthday', ?, 1, ?)
                """,
                (cur.lastrowid, birthday, created_at),
            )
        conn.commit()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api/") or path.startswith("/dear-son/api/")
    if not protected:
        return await call_next(request)
    if path in {"/api/auth/login", "/dear-son/api/auth/login", "/api/health", "/dear-son/api/health"}:
        return await call_next(request)
    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id or session_id not in SESSIONS:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    return await call_next(request)


def ensure_person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Person not found")
    return row


def get_profile(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row:
        return row
    now = now_iso()
    conn.execute(
        """
        INSERT INTO user_profile (id, first_name, default_signature_name, timezone, reminders_enabled, created_at, updated_at)
        VALUES (1, 'Mark', 'Mark', 'UTC', 1, ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()


def profile_timezone(profile_row: sqlite3.Row) -> ZoneInfo:
    tz = (profile_row["timezone"] or "UTC").strip()
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo("UTC")


def person_signature(person_row: sqlite3.Row, profile_row: sqlite3.Row) -> str:
    for candidate in [person_row["signature_name"], profile_row["default_signature_name"], profile_row["first_name"]]:
        if candidate and candidate.strip():
            return candidate.strip()
    return "Me"


def transcribe_audio(path: str) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is missing")
    client = OpenAI(api_key=OPENAI_API_KEY)
    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(model="whisper-1", file=f)
    text = getattr(result, "text", "")
    if not text:
        raise HTTPException(status_code=500, detail="Transcription failed")
    return text.strip()


def parse_bullets(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = line.lstrip("-*0123456789. ").strip()
        if line:
            lines.append(line)
    return lines[:10]


def fallback_reflection_bullets(entries: list[sqlite3.Row]) -> list[str]:
    bullets = []
    for entry in entries[:10]:
        created = parse_iso(entry["created_at"])
        prefix = created.strftime("%b %d: ") if created else ""
        text = (entry["content"] or "").strip()
        if text:
            bullets.append(f"{prefix}{text[:220]}")
    return bullets[:10]


def generate_reflection_bullets(person_row: sqlite3.Row, entries: list[sqlite3.Row]) -> list[str]:
    if not entries:
        return []
    if not ANTHROPIC_API_KEY:
        return fallback_reflection_bullets(entries)
    entries_text = "\n".join([f"- [{e['created_at']}] ({e['entry_type']}) {e['content']}" for e in entries])
    prompt = f"""You are helping create a monthly memory reflection for {person_row['name']}.
Turn the entries into 5-8 concise bullet points (hard max 10).
Rules:
- Lightly date bullets when useful (e.g. "Mar 12: ...", "Late March: ...")
- Merge repetitive entries
- Keep specifics and emotional details
- Avoid generic filler
- Return bullets only

Entries:
{entries_text}
"""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=700,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    bullets = parse_bullets("\n".join(parts).strip())
    return bullets if bullets else fallback_reflection_bullets(entries)


def reflection_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["summary_bullets"] = json.loads(data.get("summary_bullets_json") or "[]")
    except json.JSONDecodeError:
        data["summary_bullets"] = []
    data.pop("summary_bullets_json", None)
    return data


def person_milestones(conn: sqlite3.Connection, person_id: int) -> list[dict[str, Any]]:
    return dict_rows(
        conn.execute(
            "SELECT * FROM person_milestones WHERE person_id = ? ORDER BY date ASC, id ASC",
            (person_id,),
        )
    )


def normalize_milestone_payloads(items: list[dict[str, Any]] | None) -> list[MilestoneIn]:
    if not items:
        return []
    normalized: list[MilestoneIn] = []
    for item in items:
        model = MilestoneIn(**item)
        label = model.label.strip().lower()
        if label not in MILESTONE_LABELS:
            raise HTTPException(status_code=400, detail="Invalid milestone label")
        if not parse_iso(f"{model.date}T00:00:00"):
            raise HTTPException(status_code=400, detail="Invalid date format")
        normalized.append(MilestoneIn(label=label, date=model.date, is_recurring=model.is_recurring))
    return normalized


def replace_person_milestones(conn: sqlite3.Connection, person_id: int, items: list[dict[str, Any]] | None) -> None:
    milestones = normalize_milestone_payloads(items)
    conn.execute("DELETE FROM person_milestones WHERE person_id = ?", (person_id,))
    for milestone in milestones:
        conn.execute(
            """
            INSERT INTO person_milestones (person_id, label, date, is_recurring, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (person_id, milestone.label, milestone.date, int(milestone.is_recurring), now_iso()),
        )


def ensure_reflection_for_month(
    conn: sqlite3.Connection, person_row: sqlite3.Row, month_key: str, start_date: date, end_date: date
) -> dict[str, Any] | None:
    existing = conn.execute(
        "SELECT * FROM monthly_reflections WHERE person_id = ? AND month_key = ?",
        (person_row["id"], month_key),
    ).fetchone()
    entries = conn.execute(
        """
        SELECT * FROM entries
        WHERE person_id = ? AND created_at >= ? AND created_at < ?
        ORDER BY created_at DESC, id DESC
        """,
        (
            person_row["id"],
            datetime.combine(start_date, datetime.min.time()).isoformat(timespec="seconds"),
            datetime.combine(end_date, datetime.min.time()).isoformat(timespec="seconds"),
        ),
    ).fetchall()
    if not entries:
        return None
    payload = json.dumps(generate_reflection_bullets(person_row, entries))
    now = now_iso()
    if existing:
        conn.execute(
            """
            UPDATE monthly_reflections
            SET summary_bullets_json = ?, status = 'ready', updated_at = ?
            WHERE id = ?
            """,
            (payload, now, existing["id"]),
        )
        row = conn.execute("SELECT * FROM monthly_reflections WHERE id = ?", (existing["id"],)).fetchone()
    else:
        cur = conn.execute(
            """
            INSERT INTO monthly_reflections (
                person_id, month_key, summary_bullets_json, status, draft_prompted_at, draft_generated_at, created_at, updated_at
            )
            VALUES (?, ?, ?, 'ready', NULL, NULL, ?, ?)
            """,
            (person_row["id"], month_key, payload, now, now),
        )
        row = conn.execute("SELECT * FROM monthly_reflections WHERE id = ?", (cur.lastrowid,)).fetchone()
    return reflection_row_to_dict(row)


def previous_month_window(now_local: date) -> tuple[str, date, date]:
    current_start = month_start(now_local)
    prev_start = shift_months(current_start, -1)
    return month_key_for(prev_start), prev_start, current_start


def is_person_due(conn: sqlite3.Connection, person_row: sqlite3.Row, as_of: date) -> bool:
    cadence = cadence_months(person_row["letter_frequency"])
    row = conn.execute(
        """
        SELECT COALESCE(sent_at, created_at) AS sent_time
        FROM letters
        WHERE person_id = ? AND status = 'sent'
        ORDER BY COALESCE(sent_at, created_at) DESC
        LIMIT 1
        """,
        (person_row["id"],),
    ).fetchone()
    if not row or not row["sent_time"]:
        return True
    sent_dt = parse_iso(row["sent_time"])
    if not sent_dt:
        return True
    return months_between(month_start(sent_dt.date()), month_start(as_of)) >= cadence


def consecutive_blank_months(conn: sqlite3.Connection, person_id: int, anchor_month: date, cap: int = 2) -> int:
    blanks = 0
    cursor_month = anchor_month
    while blanks < cap:
        start = month_start(cursor_month)
        end = shift_months(start, 1)
        row = conn.execute(
            """
            SELECT 1
            FROM entries
            WHERE person_id = ? AND created_at >= ? AND created_at < ?
            LIMIT 1
            """,
            (
                person_id,
                datetime.combine(start, datetime.min.time()).isoformat(timespec="seconds"),
                datetime.combine(end, datetime.min.time()).isoformat(timespec="seconds"),
            ),
        ).fetchone()
        if row:
            break
        blanks += 1
        cursor_month = shift_months(start, -1)
    return blanks


def collect_current_reflections(conn: sqlite3.Connection) -> dict[str, Any]:
    profile = get_profile(conn)
    now_local = datetime.now(profile_timezone(profile)).date()
    month_key, prev_start, prev_end = previous_month_window(now_local)
    people = conn.execute(
        "SELECT * FROM people WHERE archived_at IS NULL OR archived_at = '' ORDER BY created_at ASC, id ASC"
    ).fetchall()
    reflections: list[dict[str, Any]] = []
    reengagement: list[dict[str, Any]] = []
    for person in people:
        ensured = ensure_reflection_for_month(conn, person, month_key, prev_start, prev_end)
        if ensured:
            flattened = dict(ensured)
            flattened["due_for_letter"] = is_person_due(conn, person, now_local)
            flattened["person_id"] = person["id"]
            reflections.append(flattened)
            continue
        blanks = consecutive_blank_months(conn, person["id"], prev_start, cap=2)
        if blanks >= 2:
            reengagement.append(
                {
                    "person": dict(person),
                    "blank_months": blanks,
                    "message": f"You have not logged memories for {person['name']} in two months.",
                }
            )
    conn.commit()
    return {
        "month_key": month_key,
        "month_start": prev_start.isoformat(),
        "month_end": (prev_end - timedelta(days=1)).isoformat(),
        "reflections": reflections,
        "reengagement": reengagement,
        "quiet_people": [item["person"] for item in reengagement],
    }


def relationship_context(person_row: sqlite3.Row) -> str:
    rel = person_row["relationship_type"]
    label = person_row["relationship_label"]
    return f"{rel} ({label})" if label else rel


def generate_letter_text(person_row: sqlite3.Row, profile_row: sqlite3.Row, reflection_rows: list[sqlite3.Row]) -> str:
    recipient_name = person_row["name"]
    signature = person_signature(person_row, profile_row)
    relationship = relationship_context(person_row)
    source_parts = []
    for row in reflection_rows:
        try:
            bullets = json.loads(row["summary_bullets_json"] or "[]")
        except json.JSONDecodeError:
            bullets = []
        if bullets:
            source_parts.append(f"{row['month_key']}:\n" + "\n".join([f"- {b}" for b in bullets[:10]]))
    source_text = "\n\n".join(source_parts)
    if not ANTHROPIC_API_KEY:
        return (
            f"Dear {recipient_name},\n\n"
            f"I wanted to write and reflect on this season with you. These moments mattered to me.\n\n"
            f"{source_text[:700]}\n\n"
            f"Love,\n{signature}"
        )
    prompt = f"""You ARE {profile_row['first_name']} writing a personal letter to {recipient_name} ({relationship}). Write in first person as {profile_row['first_name']}. Never refer to "{profile_row['first_name']}" in third person — you ARE the sender. Never say things like "{profile_row['first_name']}'s been telling me" or "your dad says" — you are the dad (or whoever the sender is).

The sender signs off as: {signature}

Source memories to draw from:
{source_text}

Write this letter following these rules:
1. VOICE: Casual, intimate, real — the way a person actually talks to someone they love. Never formal.
2. PERSPECTIVE: Address {recipient_name} directly as "you" throughout. Never refer to them in third person.
3. BANNED PHRASES: "I hope this letter finds you well", "Sincerely", "Best regards", "I am writing to", "I wanted to take a moment".
4. TONE: Match the relationship. For a child: warm, playful, tender — like a parent talking to their kid. For a partner: loving and personal. For a parent: respectful but warm.
5. SPECIFICS: Use the real memories from the bullets above. Don't genericize them — the details are what make the letter meaningful.
6. SIGN-OFF: Something natural. For a child: "Love you so much, {signature}" or "All my love, {signature}". Never "Sincerely".
7. FORMAT: Start with "Dear {recipient_name}," — 3-5 paragraphs — end with a warm sign-off. Do not mention AI.
"""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1200,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    text = "\n".join(parts).strip()
    if not text:
        raise HTTPException(status_code=500, detail="Letter generation failed")
    return text


def get_reflections_for_draft(conn: sqlite3.Connection, person_row: sqlite3.Row, as_of: date) -> list[sqlite3.Row]:
    sent_row = conn.execute(
        """
        SELECT COALESCE(sent_at, created_at) AS sent_time
        FROM letters
        WHERE person_id = ? AND status = 'sent'
        ORDER BY COALESCE(sent_at, created_at) DESC
        LIMIT 1
        """,
        (person_row["id"],),
    ).fetchone()
    since_month_key = None
    if sent_row and sent_row["sent_time"]:
        sent_dt = parse_iso(sent_row["sent_time"])
        if sent_dt:
            since_month_key = month_key_for(shift_months(month_start(sent_dt.date()), 1))
    as_of_month = month_key_for(month_start(as_of))
    if since_month_key:
        rows = conn.execute(
            """
            SELECT * FROM monthly_reflections
            WHERE person_id = ? AND status = 'ready' AND month_key >= ? AND month_key <= ?
            ORDER BY month_key ASC
            """,
            (person_row["id"], since_month_key, as_of_month),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM monthly_reflections
            WHERE person_id = ? AND status = 'ready' AND month_key <= ?
            ORDER BY month_key ASC
            """,
            (person_row["id"], as_of_month),
        ).fetchall()
    return list(rows)


def create_letter_record(
    conn: sqlite3.Connection,
    person_id: int,
    title: str,
    content: str,
    status: str,
    origin: str,
    source_month_start: str | None = None,
    source_month_end: str | None = None,
    parent_letter_id: int | None = None,
) -> dict[str, Any]:
    now = now_iso()
    period = source_month_end[:7] if source_month_end else datetime.utcnow().strftime("%Y-%m")
    cur = conn.execute(
        """
        INSERT INTO letters (
            person_id, title, content, status, period, source_month_start, source_month_end,
            origin, parent_letter_id, created_at, updated_at, sent_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            person_id,
            title,
            content,
            status,
            period,
            source_month_start,
            source_month_end,
            origin,
            parent_letter_id,
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM letters WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def send_via_agentmail(to_address: str, subject: str, text_body: str) -> None:
    if not AGENTMAIL_API_KEY:
        raise HTTPException(status_code=500, detail="AGENTMAIL_API_KEY is missing")
    response = requests.post(
        f"https://api.agentmail.to/v0/inboxes/{AGENTMAIL_FROM_INBOX}/messages",
        headers={"Authorization": f"Bearer {AGENTMAIL_API_KEY}"},
        json={"to": [to_address], "subject": subject, "text": text_body},
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"AgentMail error: {response.text}")


def should_send_reminder(today: date, offset_days: int) -> bool:
    next_month = shift_months(month_start(today), 1)
    return (next_month - today).days == offset_days


def send_reminder_digest(offset_days: int, force: bool = False) -> dict[str, Any]:
    with closing(get_conn()) as conn:
        profile = get_profile(conn)
        today = datetime.now(profile_timezone(profile)).date()
        if not force and not should_send_reminder(today, offset_days):
            return {"sent": False, "reason": "not_scheduled_day"}
        run_key = f"{today.isoformat()}:{offset_days}"
        seen = conn.execute("SELECT id FROM reminder_digest_log WHERE run_key = ?", (run_key,)).fetchone()
        if seen:
            return {"sent": False, "reason": "already_sent"}
        month_start_date = month_start(today)
        next_month_start = shift_months(month_start_date, 1)
        people = conn.execute(
            "SELECT * FROM people WHERE archived_at IS NULL OR archived_at = '' ORDER BY name ASC"
        ).fetchall()
        lines = []
        for person in people:
            has_entry = conn.execute(
                """
                SELECT 1 FROM entries
                WHERE person_id = ? AND created_at >= ? AND created_at < ?
                LIMIT 1
                """,
                (
                    person["id"],
                    datetime.combine(month_start_date, datetime.min.time()).isoformat(timespec="seconds"),
                    datetime.combine(next_month_start, datetime.min.time()).isoformat(timespec="seconds"),
                ),
            ).fetchone()
            if not has_entry:
                lines.append(f"- {person['name']}: no memories logged yet this month")
        if not lines:
            return {"sent": False, "reason": "nothing_to_remind"}
        target_email = OWNER_EMAIL or profile["email"]
        if not target_email:
            return {"sent": False, "reason": "OWNER_EMAIL_missing"}
        send_via_agentmail(
            target_email,
            f"Dear Ones check-in ({today.strftime('%B %Y')})",
            "You are close to month-end. Capture a few moments before the month closes.\n\n"
            + "\n".join(lines)
            + "\n\nSent via Dear Ones -- a memory letter service",
        )
        conn.execute(
            "INSERT INTO reminder_digest_log (run_key, offset_days, created_at) VALUES (?, ?, ?)",
            (run_key, offset_days, now_iso()),
        )
        conn.commit()
        return {"sent": True, "run_key": run_key, "count": len(lines)}


def _create_or_update_person(conn: sqlite3.Connection, payload: PersonIn, person_id: int | None = None) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    relationship = normalize_relationship(payload.relationship_type)
    letter_frequency = normalize_letter_frequency(relationship, payload.letter_frequency)
    relationship_label = payload.relationship_label.strip() if payload.relationship_label else None
    email = payload.email.strip() if payload.email else None
    signature_name = payload.signature_name.strip() if payload.signature_name else None
    now = now_iso()
    if person_id is None:
        cur = conn.execute(
            """
            INSERT INTO people (
                name, relationship_type, relationship_label, email, letter_frequency, signature_name, created_at, archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (name, relationship, relationship_label, email, letter_frequency, signature_name, now),
        )
        row = conn.execute("SELECT * FROM people WHERE id = ?", (cur.lastrowid,)).fetchone()
    else:
        ensure_person(conn, person_id)
        conn.execute(
            """
            UPDATE people
            SET name = ?, relationship_type = ?, relationship_label = ?, email = ?, letter_frequency = ?, signature_name = ?
            WHERE id = ?
            """,
            (name, relationship, relationship_label, email, letter_frequency, signature_name, person_id),
        )
        row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    return dict(row)


api = APIRouter()
legacy_compat = APIRouter()


@api.get("/health")
def health():
    return {"ok": True}


@api.post("/auth/login")
def login(payload: LoginIn, response: Response):
    if payload.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    session_id = secrets.token_urlsafe(32)
    SESSIONS.add(session_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        path=COOKIE_PATH,
        secure=False,
    )
    return {"ok": True}


@api.post("/auth/logout")
def logout(request: Request, response: Response):
    session_id = request.cookies.get(COOKIE_NAME)
    if session_id:
        SESSIONS.discard(session_id)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return {"ok": True}


@api.get("/profile")
def get_profile_api():
    with closing(get_conn()) as conn:
        return dict(get_profile(conn))


@api.put("/profile")
def update_profile(payload: ProfileUpdateIn):
    with closing(get_conn()) as conn:
        existing = get_profile(conn)
        first_name = payload.first_name.strip() if payload.first_name is not None else existing["first_name"]
        default_signature_name = (
            payload.default_signature_name.strip()
            if payload.default_signature_name is not None
            else existing["default_signature_name"]
        )
        timezone_name = payload.timezone.strip() if payload.timezone is not None else existing["timezone"]
        try:
            ZoneInfo(timezone_name)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid timezone")
        reminders_enabled = (
            int(payload.reminders_enabled) if payload.reminders_enabled is not None else existing["reminders_enabled"]
        )
        email = payload.email.strip() if payload.email is not None else existing["email"]
        conn.execute(
            """
            UPDATE user_profile
            SET first_name = ?, email = ?, default_signature_name = ?, timezone = ?, reminders_enabled = ?, updated_at = ?
            WHERE id = 1
            """,
            (first_name, email, default_signature_name, timezone_name, reminders_enabled, now_iso()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone())


@api.get("/people")
def get_people():
    with closing(get_conn()) as conn:
        profile = get_profile(conn)
        today = datetime.now(profile_timezone(profile)).date()
        rows = conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM entries e WHERE e.person_id = p.id) AS entry_count,
                   (SELECT MAX(created_at) FROM entries e WHERE e.person_id = p.id) AS last_entry_at
            FROM people p
            ORDER BY p.created_at ASC, p.id ASC
            """
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["due_for_letter"] = is_person_due(conn, row, today)
            item["blank_months"] = consecutive_blank_months(conn, row["id"], month_start(today), cap=2)
            item["milestones"] = person_milestones(conn, row["id"])
            out.append(item)
        return out


@api.post("/people")
def create_person(payload: PersonIn):
    with closing(get_conn()) as conn:
        row = _create_or_update_person(conn, payload)
        replace_person_milestones(conn, row["id"], payload.milestones)
        conn.commit()
        row["milestones"] = person_milestones(conn, row["id"])
        return row


@api.get("/people/{person_id}")
def get_person(person_id: int):
    with closing(get_conn()) as conn:
        item = dict(ensure_person(conn, person_id))
        item["milestones"] = person_milestones(conn, person_id)
        return item


@api.put("/people/{person_id}")
def update_person(person_id: int, payload: PersonUpdateIn):
    with closing(get_conn()) as conn:
        existing = ensure_person(conn, person_id)
        model = PersonIn(
            name=payload.name if payload.name is not None else existing["name"],
            relationship_type=payload.relationship_type if payload.relationship_type is not None else existing["relationship_type"],
            relationship_label=payload.relationship_label
            if payload.relationship_label is not None
            else existing["relationship_label"],
            email=payload.email if payload.email is not None else existing["email"],
            letter_frequency=payload.letter_frequency if payload.letter_frequency is not None else existing["letter_frequency"],
            signature_name=payload.signature_name if payload.signature_name is not None else existing["signature_name"],
            milestones=payload.milestones,
        )
        row = _create_or_update_person(conn, model, person_id=person_id)
        if payload.archived_at is not None:
            conn.execute("UPDATE people SET archived_at = ? WHERE id = ?", (payload.archived_at, person_id))
            row = dict(conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())
        if payload.milestones is not None:
            replace_person_milestones(conn, person_id, payload.milestones)
            row["milestones"] = person_milestones(conn, person_id)
        else:
            row["milestones"] = person_milestones(conn, person_id)
        conn.commit()
        return row


@api.get("/people/{person_id}/milestones")
def get_person_milestones(person_id: int):
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
        return dict_rows(
            conn.execute(
                "SELECT * FROM person_milestones WHERE person_id = ? ORDER BY date ASC, id ASC",
                (person_id,),
            )
        )


@api.post("/people/{person_id}/milestones")
def create_person_milestone(person_id: int, payload: MilestoneIn):
    label = payload.label.strip().lower()
    if label not in MILESTONE_LABELS:
        raise HTTPException(status_code=400, detail="Invalid milestone label")
    if not parse_iso(f"{payload.date}T00:00:00"):
        raise HTTPException(status_code=400, detail="Invalid date format")
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO person_milestones (person_id, label, date, is_recurring, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (person_id, label, payload.date, int(payload.is_recurring), now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM person_milestones WHERE id = ?", (cur.lastrowid,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM person_milestones WHERE person_id = ? AND label = ? AND date = ?",
                (person_id, label, payload.date),
            ).fetchone()
        return dict(row)


@api.get("/people/{person_id}/entries")
def get_entries(person_id: int):
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
        return dict_rows(
            conn.execute(
                "SELECT * FROM entries WHERE person_id = ? ORDER BY created_at DESC, id DESC",
                (person_id,),
            )
        )


@api.post("/people/{person_id}/entries")
def create_entry(person_id: int, payload: EntryIn):
    if payload.entry_type not in {"text", "voice"}:
        raise HTTPException(status_code=400, detail="entry_type must be text or voice")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
        cur = conn.execute(
            """
            INSERT INTO entries (person_id, content, entry_type, tags, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (person_id, payload.content.strip(), payload.entry_type, payload.tags, now_iso()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone())


@api.post("/people/{person_id}/entries/voice")
async def create_voice_entry(person_id: int, audio: UploadFile = File(...)):
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
    suffix = Path(audio.filename or "voice.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp") as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name
    try:
        transcription = transcribe_audio(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
    with closing(get_conn()) as conn:
        cur = conn.execute(
            """
            INSERT INTO entries (person_id, content, entry_type, tags, created_at)
            VALUES (?, ?, 'voice', NULL, ?)
            """,
            (person_id, transcription, now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"entry": dict(row), "transcription": transcription}


@api.get("/people/{person_id}/letters")
def get_letters(person_id: int):
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
        return dict_rows(
            conn.execute(
                "SELECT * FROM letters WHERE person_id = ? ORDER BY created_at DESC, id DESC",
                (person_id,),
            )
        )


@api.post("/people/{person_id}/letters")
def create_letter(person_id: int, payload: NewLetterIn):
    origin = payload.origin.strip().lower()
    if origin not in {"manual", "dictated", "ai_draft", "polished"}:
        raise HTTPException(status_code=400, detail="Invalid origin")
    with closing(get_conn()) as conn:
        person = ensure_person(conn, person_id)
        profile = get_profile(conn)
        signature = person_signature(person, profile)
        letter = create_letter_record(
            conn,
            person_id=person_id,
            title=payload.title or f"Letter - {datetime.utcnow().strftime('%B %Y')}",
            content=payload.content or f"Dear {person['name']},\n\n\nLove,\n{signature}",
            status="draft",
            origin=origin,
        )
        conn.commit()
        return letter


@api.post("/people/{person_id}/generate-letter")
def generate_letter(person_id: int, payload: GenerateLetterIn | None = None):
    with closing(get_conn()) as conn:
        person = ensure_person(conn, person_id)
        profile = get_profile(conn)
        reflections = get_reflections_for_draft(conn, person, datetime.now(profile_timezone(profile)).date())
        if payload and payload.reflection_id:
            filtered = [r for r in reflections if r["id"] == payload.reflection_id]
            if filtered:
                reflections = filtered
        if not reflections:
            raise HTTPException(status_code=400, detail="No reflection data available for letter generation")
        letter = create_letter_record(
            conn,
            person_id=person_id,
            title=f"Letter - {datetime.utcnow().strftime('%B %Y')}",
            content=generate_letter_text(person, profile, reflections),
            status="draft",
            origin="ai_draft",
            source_month_start=f"{reflections[0]['month_key']}-01",
            source_month_end=f"{reflections[-1]['month_key']}-01",
        )
        now = now_iso()
        conn.execute(
            """
            UPDATE monthly_reflections
            SET draft_prompted_at = COALESCE(draft_prompted_at, ?), draft_generated_at = ?
            WHERE person_id = ? AND month_key >= ? AND month_key <= ?
            """,
            (now, now, person_id, reflections[0]["month_key"], reflections[-1]["month_key"]),
        )
        conn.commit()
        return letter


@api.get("/people/{person_id}/reflections")
def get_person_reflections(person_id: int):
    with closing(get_conn()) as conn:
        ensure_person(conn, person_id)
        rows = conn.execute(
            "SELECT * FROM monthly_reflections WHERE person_id = ? ORDER BY month_key DESC",
            (person_id,),
        ).fetchall()
        return [reflection_row_to_dict(r) for r in rows]


@api.get("/reflections/current")
def get_current_reflections():
    with closing(get_conn()) as conn:
        return collect_current_reflections(conn)


@api.post("/reflections/{reflection_id}/generate-letter")
def generate_letter_from_reflection(reflection_id: int):
    with closing(get_conn()) as conn:
        reflection = conn.execute("SELECT * FROM monthly_reflections WHERE id = ?", (reflection_id,)).fetchone()
        if not reflection:
            raise HTTPException(status_code=404, detail="Reflection not found")
        person = ensure_person(conn, reflection["person_id"])
        profile = get_profile(conn)
        reflections = get_reflections_for_draft(conn, person, datetime.now(profile_timezone(profile)).date()) or [reflection]
        letter = create_letter_record(
            conn,
            person_id=person["id"],
            title=f"Letter - {datetime.utcnow().strftime('%B %Y')}",
            content=generate_letter_text(person, profile, reflections),
            status="draft",
            origin="ai_draft",
            source_month_start=f"{reflections[0]['month_key']}-01",
            source_month_end=f"{reflections[-1]['month_key']}-01",
        )
        now = now_iso()
        conn.execute(
            """
            UPDATE monthly_reflections
            SET draft_prompted_at = COALESCE(draft_prompted_at, ?), draft_generated_at = ?
            WHERE person_id = ? AND month_key >= ? AND month_key <= ?
            """,
            (now, now, person["id"], reflections[0]["month_key"], reflections[-1]["month_key"]),
        )
        conn.commit()
        return letter


@api.post("/letters/{letter_id}/polish")
def polish_letter(letter_id: int, payload: PolishLetterIn):
    with closing(get_conn()) as conn:
        letter = conn.execute("SELECT * FROM letters WHERE id = ?", (letter_id,)).fetchone()
        if not letter:
            raise HTTPException(status_code=404, detail="Letter not found")
        person = ensure_person(conn, letter["person_id"])
        profile = get_profile(conn)
        source_content = payload.content if payload.content is not None else (letter["content"] or "")
        if not source_content.strip():
            raise HTTPException(status_code=400, detail="No content to polish")
        if ANTHROPIC_API_KEY:
            rel = relationship_context(person_row=person)
            sig = person_signature(person, profile)
            prompt = f"""You ARE {profile['first_name']} rewriting your own letter to {person['name']} ({rel}). Write in first person as {profile['first_name']} — you are the sender, not a ghostwriter describing them. Never refer to "{profile['first_name']}" in third person.
Sender signs off as: {sig}
Optional instruction: {payload.note or 'none'}

Rewrite the letter below following these rules strictly:

1. VOICE: Casual, warm, intimate — never formal. Ban these phrases: "I hope this letter finds you well", "Sincerely", "Best regards", "I wanted to take a moment", "I am writing to".
2. PERSPECTIVE: Always address {person['name']} directly as "you" / "your". If the draft refers to {person['name']} in third person ("he said", "she did"), convert it to second person ("you said", "you did").
3. SIGN-OFF: Natural for this relationship. For a child: "Love you so much", "All my love, {sig}", "Love always, {sig}". Never "Sincerely" or formal closings.
4. TONE: Match the relationship type ({rel}). For a child: playful, tender, like a parent talking directly to their kid. Warm and real, not polished corporate prose.
5. STRUCTURE: Keep it as a real letter — "Dear {person['name']}," opening, 3-5 natural paragraphs, warm sign-off. Don't pad or genericize.
6. KEEP THE SPECIFICS: Preserve all real memories, details, and moments from the original. Those are the heart of the letter.

Letter to rewrite:
{source_content}
"""
            client = Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1200,
                temperature=0.7,
                messages=[{"role": "user", "content": prompt}],
            )
            polished = "\n".join(
                [block.text for block in response.content if getattr(block, "type", "") == "text"]
            ).strip()
            if not polished:
                polished = source_content
        else:
            polished = source_content
        created = create_letter_record(
            conn,
            person_id=person["id"],
            title=(letter["title"] or "Untitled letter") + " (Polished)",
            content=polished,
            status="draft",
            origin="polished",
            source_month_start=letter["source_month_start"],
            source_month_end=letter["source_month_end"],
            parent_letter_id=letter["id"],
        )
        conn.commit()
        return created


@api.put("/letters/{letter_id}")
def update_letter(letter_id: int, payload: LetterUpdateIn):
    fields = []
    params: list[Any] = []
    if payload.content is not None:
        fields.append("content = ?")
        params.append(payload.content)
    if payload.status is not None:
        status = payload.status.strip().lower()
        if status not in {"draft", "sealed", "sent"}:
            raise HTTPException(status_code=400, detail="Invalid status")
        fields.append("status = ?")
        params.append(status)
        if status == "sent":
            fields.append("sent_at = COALESCE(sent_at, ?)")
            params.append(now_iso())
    if payload.title is not None:
        fields.append("title = ?")
        params.append(payload.title)
    if not fields:
        raise HTTPException(status_code=400, detail="No updates provided")
    fields.append("updated_at = ?")
    params.append(now_iso())
    with closing(get_conn()) as conn:
        if not conn.execute("SELECT id FROM letters WHERE id = ?", (letter_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Letter not found")
        params.append(letter_id)
        conn.execute(f"UPDATE letters SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        return dict(conn.execute("SELECT * FROM letters WHERE id = ?", (letter_id,)).fetchone())


@api.post("/letters/{letter_id}/send")
def send_letter(letter_id: int):
    with closing(get_conn()) as conn:
        letter = conn.execute("SELECT * FROM letters WHERE id = ?", (letter_id,)).fetchone()
        if not letter:
            raise HTTPException(status_code=404, detail="Letter not found")
        person = ensure_person(conn, letter["person_id"])
        if not person["email"]:
            raise HTTPException(status_code=400, detail="Person email is missing")
        profile = get_profile(conn)
        signature = person_signature(person, profile)
    body = (letter["content"] or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Letter content is empty")
    send_via_agentmail(
        person["email"],
        f"A letter for {person['name']} via Dear Ones",
        f"{body}\n\nLove,\n{signature}\n\nSent via Dear Ones -- a memory letter service",
    )
    with closing(get_conn()) as conn:
        sent_at = now_iso()
        conn.execute(
            "UPDATE letters SET status = 'sent', sent_at = ?, updated_at = ? WHERE id = ?",
            (sent_at, sent_at, letter_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM letters WHERE id = ?", (letter_id,)).fetchone()
        return {"ok": True, "letter": dict(updated)}


@legacy_compat.get("/children")
def legacy_get_children():
    with closing(get_conn()) as conn:
        rows = dict_rows(
            conn.execute(
                """
                SELECT p.*,
                       (SELECT COUNT(*) FROM entries e WHERE e.person_id = p.id) AS entry_count,
                       (SELECT MAX(created_at) FROM entries e WHERE e.person_id = p.id) AS last_entry_at
                FROM people p
                ORDER BY p.created_at ASC, p.id ASC
                """
            )
        )
        for row in rows:
            birthday = conn.execute(
                """
                SELECT date FROM person_milestones
                WHERE person_id = ? AND label = 'birthday'
                ORDER BY date ASC
                LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            row["dob"] = birthday["date"] if birthday else None
        return rows


@legacy_compat.post("/children")
def legacy_create_child(payload: dict[str, Any]):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    person_payload = PersonIn(
        name=name,
        relationship_type="child",
        email=payload.get("email"),
        letter_frequency="monthly",
    )
    dob = (payload.get("dob") or "").strip()
    with closing(get_conn()) as conn:
        person = _create_or_update_person(conn, person_payload)
        if dob:
            conn.execute(
                """
                INSERT OR IGNORE INTO person_milestones (person_id, label, date, is_recurring, created_at)
                VALUES (?, 'birthday', ?, 1, ?)
                """,
                (person["id"], dob, now_iso()),
            )
        conn.commit()
        return person


@legacy_compat.get("/children/{child_id}/entries")
def legacy_get_entries(child_id: int):
    return get_entries(child_id)


@legacy_compat.post("/children/{child_id}/entries")
def legacy_create_entry(child_id: int, payload: EntryIn):
    return create_entry(child_id, payload)


@legacy_compat.post("/children/{child_id}/entries/voice")
async def legacy_create_voice_entry(child_id: int, audio: UploadFile = File(...)):
    return await create_voice_entry(child_id, audio)


@legacy_compat.get("/children/{child_id}/letters")
def legacy_get_letters(child_id: int):
    return get_letters(child_id)


@legacy_compat.post("/children/{child_id}/generate-letter")
def legacy_generate_letter(child_id: int, payload: GenerateLetterIn | None = None):
    return generate_letter(child_id, payload)


@app.get("/", include_in_schema=False)
def index_root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/dear-son", include_in_schema=False)
def index_legacy_short():
    return RedirectResponse(url="/dear-son/")


@app.get("/dear-son/", include_in_schema=False)
def index_legacy():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.on_event("startup")
def startup_event():
    init_db()
    seed_data()


app.include_router(api, prefix="/api")
app.include_router(api, prefix="/dear-son/api")
app.include_router(legacy_compat, prefix="/dear-son/api")


def run_generate_reflections() -> dict[str, Any]:
    with closing(get_conn()) as conn:
        data = collect_current_reflections(conn)
    return {
        "month_key": data["month_key"],
        "reflection_count": len(data["reflections"]),
        "reengagement_count": len(data["reengagement"]),
    }


def run_send_reminders(offset_days: int, force: bool) -> dict[str, Any]:
    return send_reminder_digest(offset_days=offset_days, force=force)


def run_cli() -> int:
    parser = argparse.ArgumentParser(description="Dear Ones maintenance tasks")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("generate-reflections", help="Generate prior-month reflections")
    reminders_parser = subparsers.add_parser("send-reminders", help="Send reminder digest")
    reminders_parser.add_argument("--offset-days", type=int, default=7, choices=[7, 3])
    reminders_parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1
    init_db()
    if args.command == "generate-reflections":
        result = run_generate_reflections()
    elif args.command == "send-reminders":
        result = run_send_reminders(offset_days=args.offset_days, force=args.force)
    else:
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
