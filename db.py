"""
PostgreSQL data access layer (Supabase-compatible).

When DATABASE_URL is set, all data functions operate against PostgreSQL
instead of local JSON files. Uses psycopg2 with thread-local connections
to work safely with ThreadingMixIn.
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

# Auto-install psycopg2-binary if missing (handles Render's cached builds)
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("[DB] psycopg2 not found, installing...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary"])
    import psycopg2
    from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Thread-local storage for connections
_local = threading.local()


def _get_conn():
    """Get or create a thread-local database connection."""
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        _local.conn = conn
    return conn


def _execute(sql, params=None, fetch=False, fetchone=False):
    """Execute SQL with automatic reconnection on failure."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetchone:
                result = cur.fetchone()
            elif fetch:
                result = cur.fetchall()
            else:
                result = None
            conn.commit()
            return result
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Connection lost — reconnect and retry once
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
        conn = _get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetchone:
                result = cur.fetchone()
            elif fetch:
                result = cur.fetchall()
            else:
                result = None
            conn.commit()
            return result


# ─────────────────────────── Schema Init ──────────────────────────────


def init_db():
    """Create tables if they do not exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
        username    TEXT PRIMARY KEY,
        salt        TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS todos (
        id          SERIAL PRIMARY KEY,
        username    TEXT NOT NULL,
        todo_id     INTEGER NOT NULL,
        text        TEXT NOT NULL DEFAULT '',
        tag         TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'pending',
        date        TEXT NOT NULL DEFAULT '',
        note        TEXT NOT NULL DEFAULT '',
        starred     BOOLEAN NOT NULL DEFAULT FALSE,
        reminder    TEXT,
        reminder_target TEXT NOT NULL DEFAULT '',
        progress    JSONB NOT NULL DEFAULT '[]'::jsonb,
        attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
        sort_order  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_todos_username ON todos(username);

    CREATE TABLE IF NOT EXISTS tags (
        id          SERIAL PRIMARY KEY,
        username    TEXT NOT NULL,
        tag         TEXT NOT NULL,
        sort_order  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_tags_username ON tags(username);

    CREATE TABLE IF NOT EXISTS contacts (
        id          SERIAL PRIMARY KEY,
        username    TEXT NOT NULL,
        name        TEXT NOT NULL DEFAULT '',
        erp         TEXT NOT NULL DEFAULT '',
        type        TEXT NOT NULL DEFAULT 'person',
        sort_order  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_contacts_username ON contacts(username);

    CREATE TABLE IF NOT EXISTS uploads (
        id          SERIAL PRIMARY KEY,
        username    TEXT NOT NULL,
        filename    TEXT NOT NULL,
        mime_type   TEXT NOT NULL DEFAULT 'application/octet-stream',
        file_data   BYTEA NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_uploads_username_filename ON uploads(username, filename);
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    print("[DB] Tables initialized.")


# ─────────────────────────── Users ────────────────────────────────────


def db_load_users() -> Dict[str, Any]:
    """Load all users as {username: {salt, hash, created}} dict."""
    rows = _execute("SELECT username, salt, password_hash, created_at FROM users", fetch=True)
    result = {}
    for r in rows:
        result[r["username"]] = {
            "salt": r["salt"],
            "hash": r["password_hash"],
            "created": r["created_at"].isoformat() if r["created_at"] else "",
        }
    return result


def db_save_users(users: Dict[str, Any]) -> None:
    """Sync full users dict to database (upsert all)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for username, info in users.items():
                cur.execute("""
                    INSERT INTO users (username, salt, password_hash, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (username) DO UPDATE
                        SET salt = EXCLUDED.salt,
                            password_hash = EXCLUDED.password_hash
                """, (
                    username,
                    info.get("salt", ""),
                    info.get("hash", ""),
                    info.get("created", datetime.now().isoformat()),
                ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─────────────────────────── Todos ────────────────────────────────────


def db_load_todos(username=None) -> List[Dict[str, Any]]:
    """Load todos for a user (or all if username is None for legacy compat)."""
    if username:
        rows = _execute(
            "SELECT * FROM todos WHERE username = %s ORDER BY sort_order ASC",
            (username,), fetch=True
        )
    else:
        rows = _execute("SELECT * FROM todos ORDER BY sort_order ASC", fetch=True)

    todos = []
    for r in rows:
        todos.append({
            "id": r["todo_id"],
            "text": r["text"],
            "tag": r["tag"],
            "status": r["status"],
            "date": r["date"],
            "note": r["note"],
            "starred": r["starred"],
            "reminder": r["reminder"],
            "reminder_target": r["reminder_target"],
            "progress": r["progress"] if isinstance(r["progress"], list) else json.loads(r["progress"] or "[]"),
            "attachments": r["attachments"] if isinstance(r["attachments"], list) else json.loads(r["attachments"] or "[]"),
        })
    return todos


def db_save_todos(todos: List[Dict[str, Any]], username=None) -> None:
    """Save todos for a user — atomic DELETE + INSERT in a transaction."""
    if not username:
        # Legacy global mode: skip DB or handle as needed
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM todos WHERE username = %s", (username,))
            for idx, t in enumerate(todos):
                cur.execute("""
                    INSERT INTO todos (username, todo_id, text, tag, status, date, note,
                                       starred, reminder, reminder_target, progress, attachments, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    username,
                    t.get("id", 0),
                    t.get("text", ""),
                    t.get("tag", ""),
                    t.get("status", "pending"),
                    t.get("date", ""),
                    t.get("note", ""),
                    t.get("starred", False),
                    t.get("reminder"),
                    t.get("reminder_target", ""),
                    json.dumps(t.get("progress", []), ensure_ascii=False),
                    json.dumps(t.get("attachments", []), ensure_ascii=False),
                    idx,
                ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─────────────────────────── Tags ─────────────────────────────────────

DEFAULT_TAGS = [
    "运营工作", "降本运营", "数据相关", "综合统筹", "大满贯",
    "合规整改消保", "加速landing", "绩效汇报", "SOP", "AIAI",
]


def db_load_tags(username=None) -> List[str]:
    """Load tags for a user. If none exist, insert defaults and return them."""
    if not username:
        return list(DEFAULT_TAGS)
    rows = _execute(
        "SELECT tag FROM tags WHERE username = %s ORDER BY sort_order ASC",
        (username,), fetch=True
    )
    if rows:
        return [r["tag"] for r in rows]
    # No tags yet — write defaults
    db_save_tags(DEFAULT_TAGS, username)
    return list(DEFAULT_TAGS)


def db_save_tags(tags: List[str], username=None) -> None:
    """Save tags for a user — atomic DELETE + INSERT."""
    if not username:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tags WHERE username = %s", (username,))
            for idx, tag in enumerate(tags):
                cur.execute(
                    "INSERT INTO tags (username, tag, sort_order) VALUES (%s, %s, %s)",
                    (username, tag, idx)
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─────────────────────────── Contacts ─────────────────────────────────

DEFAULT_CONTACTS = [
    {"name": "陈实", "erp": "chenshi23", "type": "person"},
    {"name": "冯汉禹", "erp": "fenghanyu.3", "type": "person"},
    {"name": "高志华", "erp": "gaozhihua9", "type": "person"},
    {"name": "黄萌", "erp": "huangmeng16", "type": "person"},
    {"name": "姜海燕", "erp": "jianghaiyan1", "type": "person"},
    {"name": "李雪", "erp": "lixue368", "type": "person"},
    {"name": "苏星宇", "erp": "suxingyu7", "type": "person"},
    {"name": "孙雅宜", "erp": "sunyayi.1", "type": "person"},
    {"name": "王峰", "erp": "wangfeng479", "type": "person"},
    {"name": "肖伶俐", "erp": "xiaolingli", "type": "person"},
    {"name": "叶帆", "erp": "yefan23", "type": "person"},
    {"name": "郑犇犇", "erp": "zhengbenben", "type": "person"},
    {"name": "郑润宸", "erp": "zhengrunchen.1", "type": "person"},
    {"name": "朱贺存", "erp": "zhuhecun1", "type": "person"},
    {"name": "毛宇君", "erp": "maoyujun.3", "type": "person"},
    {"name": "祝显荣", "erp": "zhuxianrong.0328", "type": "person"},
    {"name": "沈延德", "erp": "shenyande.1", "type": "person"},
    {"name": "🍚 干饭人", "erp": "group:10218887128", "type": "group"},
    {"name": "🏢 基础产品部", "erp": "group:1024703302", "type": "group"},
]


def db_load_contacts(username=None) -> List[Dict[str, Any]]:
    """Load contacts for a user. If none exist, insert defaults and return them."""
    if not username:
        return list(DEFAULT_CONTACTS)
    rows = _execute(
        "SELECT name, erp, type FROM contacts WHERE username = %s ORDER BY sort_order ASC",
        (username,), fetch=True
    )
    if rows:
        return [dict(r) for r in rows]
    # No contacts yet — write defaults
    db_save_contacts(DEFAULT_CONTACTS, username)
    return list(DEFAULT_CONTACTS)


def db_save_contacts(contacts: List[Dict[str, Any]], username=None) -> None:
    """Save contacts for a user — atomic DELETE + INSERT."""
    if not username:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM contacts WHERE username = %s", (username,))
            for idx, c in enumerate(contacts):
                cur.execute(
                    "INSERT INTO contacts (username, name, erp, type, sort_order) VALUES (%s, %s, %s, %s, %s)",
                    (username, c.get("name", ""), c.get("erp", ""), c.get("type", "person"), idx)
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─────────────────────────── Uploads ──────────────────────────────────


def db_save_upload(username: str, filename: str, mime_type: str, file_data: bytes) -> None:
    """Store uploaded file binary in the database."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO uploads (username, filename, mime_type, file_data)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (username, filename, mime_type, psycopg2.Binary(file_data)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def db_load_upload(username: str, filename: str) -> Optional[tuple]:
    """Load a file from the database. Returns (mime_type, file_data) or None."""
    row = _execute(
        "SELECT mime_type, file_data FROM uploads WHERE username = %s AND filename = %s",
        (username, filename), fetchone=True
    )
    if row:
        return (row["mime_type"], bytes(row["file_data"]))
    return None


# ─────────────────────────── Migration ────────────────────────────────


def migrate_json_to_db(data_dir: str) -> None:
    """One-time migration: read existing JSON files from data/ and insert into DB.

    Triggered by MIGRATE_JSON_TO_DB=1 environment variable.
    """
    print("[Migration] Starting JSON → PostgreSQL migration...")

    # Migrate users
    users_path = os.path.join(data_dir, "users.json")
    if os.path.exists(users_path):
        with open(users_path, "r", encoding="utf-8") as f:
            users = json.load(f)
        db_save_users(users)
        print(f"[Migration] Migrated {len(users)} users.")

    # Migrate per-user data
    if not os.path.isdir(data_dir):
        print("[Migration] No data directory found, nothing to migrate.")
        return

    for username in os.listdir(data_dir):
        user_dir = os.path.join(data_dir, username)
        if not os.path.isdir(user_dir) or username == "__pycache__":
            continue

        # Todos
        todos_path = os.path.join(user_dir, "todos.json")
        if os.path.exists(todos_path):
            with open(todos_path, "r", encoding="utf-8") as f:
                todos = json.load(f)
            if todos:
                db_save_todos(todos, username)
                print(f"[Migration]   {username}: {len(todos)} todos")

        # Tags
        tags_path = os.path.join(user_dir, "tags.json")
        if os.path.exists(tags_path):
            with open(tags_path, "r", encoding="utf-8") as f:
                tags = json.load(f)
            if tags:
                db_save_tags(tags, username)
                print(f"[Migration]   {username}: {len(tags)} tags")

        # Contacts
        contacts_path = os.path.join(user_dir, "contacts.json")
        if os.path.exists(contacts_path):
            with open(contacts_path, "r", encoding="utf-8") as f:
                contacts = json.load(f)
            if contacts:
                db_save_contacts(contacts, username)
                print(f"[Migration]   {username}: {len(contacts)} contacts")

        # Uploads
        uploads_dir = os.path.join(user_dir, "uploads")
        if os.path.isdir(uploads_dir):
            import mimetypes
            count = 0
            for fname in os.listdir(uploads_dir):
                fpath = os.path.join(uploads_dir, fname)
                if os.path.isfile(fpath):
                    mime, _ = mimetypes.guess_type(fpath)
                    if mime is None:
                        mime = "application/octet-stream"
                    with open(fpath, "rb") as f:
                        data = f.read()
                    db_save_upload(username, fname, mime, data)
                    count += 1
            if count:
                print(f"[Migration]   {username}: {count} uploads")

    print("[Migration] Done.")


# ─────────────────────────── DB → JSON Export (for GitHub backup) ─────


def export_db_to_json(data_dir: str) -> None:
    """Export all database content back to JSON files in data/ for GitHub backup."""
    os.makedirs(data_dir, exist_ok=True)

    # Export users
    users = db_load_users()
    if users:
        users_path = os.path.join(data_dir, "users.json")
        with open(users_path, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)

    # Export per-user data
    for username in users.keys():
        user_dir = os.path.join(data_dir, username)
        os.makedirs(user_dir, exist_ok=True)

        # Todos
        todos = db_load_todos(username)
        with open(os.path.join(user_dir, "todos.json"), "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)

        # Tags
        tags = db_load_tags(username)
        with open(os.path.join(user_dir, "tags.json"), "w", encoding="utf-8") as f:
            json.dump(tags, f, ensure_ascii=False, indent=2)

        # Contacts
        contacts = db_load_contacts(username)
        with open(os.path.join(user_dir, "contacts.json"), "w", encoding="utf-8") as f:
            json.dump(contacts, f, ensure_ascii=False, indent=2)
