from pathlib import Path
import sqlite3
import time
import uuid
from typing import Optional

from config import DEFAULT_MODEL
from schemas import ApiProfileBody, MessageItem

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "chat.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def add_column_if_missing(cur, table: str, column: str, definition: str):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        file_name TEXT,
        image_preview TEXT,
        file_context TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id)
    )
    """)


    for table, column, definition in [
        ("messages", "model", "TEXT"),
        ("messages", "provider_name", "TEXT"),
        ("messages", "token_count", "INTEGER"),
        ("messages", "file_context", "TEXT"),
        ("messages", "superseded_by", "INTEGER"),
        ("conversations", "is_pinned", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        add_column_if_missing(cur, table, column, definition)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS api_profiles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        auth_token TEXT NOT NULL,
        model TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_profiles_updated_at ON api_profiles(updated_at)")
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"DB created: {DB_PATH}")


def ensure_api_profiles_route_mode_column(conn):
    try:
        add_column_if_missing(conn, "api_profiles", "route_mode", "TEXT DEFAULT 'direct'")
        conn.commit()
    except Exception:
        pass


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def db_create_conversation(title: str = "新对话") -> str:
    cid = new_id()
    ts = now_ms()
    conn = get_conn()
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (cid, title or "新对话", ts, ts),
    )
    conn.commit()
    conn.close()
    return cid


def db_ensure_conversation(cid: str, title: str = "新对话") -> str:
    if cid:
        conn = get_conn()
        row = conn.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
        if row:
            conn.close()
            return cid
        conn.close()

    return db_create_conversation(title)


def db_add_message(
    conversation_id: str,
    role: str,
    content: str,
    file_name: Optional[str] = None,
    image_preview: Optional[str] = None,
    file_context: Optional[str] = None,
    model: Optional[str] = None,
    provider_name: Optional[str] = None,
    token_count: Optional[int] = None,
    superseded_by: Optional[int] = None,
):
    ts = now_ms()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO messages (conversation_id, role, content, file_name, image_preview, file_context, model, provider_name, token_count, superseded_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (conversation_id, role, content or "", file_name, image_preview, file_context, model, provider_name, token_count, superseded_by, ts),
    )
    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?",
        (ts, conversation_id),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def db_update_title_if_needed(conversation_id: str, title_source: str):
    conn = get_conn()
    row = conn.execute("SELECT title FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if row and (row["title"].startswith("新对话") or row["title"].strip() == ""):
        title = (title_source or "新对话").strip().replace("\n", " ")[:18] or "新对话"
        conn.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
            (title, now_ms(), conversation_id),
        )
        conn.commit()
    conn.close()


def db_list_api_profiles():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, base_url, auth_token, model, is_default, created_at, updated_at FROM api_profiles ORDER BY is_default DESC, updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def db_save_api_profile(profile_id: str, body: ApiProfileBody):
    ts = now_ms()
    pid = profile_id or new_id()

    conn = get_conn()

    if body.is_default:
        conn.execute("UPDATE api_profiles SET is_default=0")

    old = conn.execute("SELECT id FROM api_profiles WHERE id=?", (pid,)).fetchone()

    if old:
        conn.execute(
            """
            UPDATE api_profiles
            SET name=?, base_url=?, auth_token=?, model=?, is_default=?, updated_at=?
            WHERE id=?
            """,
            (
                body.name.strip() or "未命名接入商",
                body.base_url.strip(),
                body.auth_token.strip(),
                body.model.strip() or DEFAULT_MODEL,
                1 if body.is_default else 0,
                ts,
                pid,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO api_profiles
            (id, name, base_url, auth_token, model, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                body.name.strip() or "未命名接入商",
                body.base_url.strip(),
                body.auth_token.strip(),
                body.model.strip() or DEFAULT_MODEL,
                1 if body.is_default else 0,
                ts,
                ts,
            ),
        )

    conn.commit()
    conn.close()
    return pid


def db_delete_api_profile(profile_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM api_profiles WHERE id=?", (profile_id,))
    conn.commit()
    conn.close()


def db_set_default_api_profile(profile_id: str):
    conn = get_conn()
    conn.execute("UPDATE api_profiles SET is_default=0")
    conn.execute(
        "UPDATE api_profiles SET is_default=1, updated_at=? WHERE id=?",
        (now_ms(), profile_id),
    )
    conn.commit()
    conn.close()


def db_delete_last_assistant_message(conversation_id: str):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT id FROM messages
        WHERE conversation_id=? AND role='assistant'
        ORDER BY id DESC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()

    if row:
        conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        conn.commit()

    conn.close()


def message_item_from_row(row) -> MessageItem:
    keys = row.keys()
    return MessageItem(
        id=row["id"],
        role=row["role"],
        content=row["content"],
        fileName=row["file_name"],
        imagePreview=row["image_preview"],
        fileContext=row["file_context"] if "file_context" in keys else None,
        model=row["model"] if "model" in keys else None,
        providerName=row["provider_name"] if "provider_name" in keys else None,
        tokenCount=row["token_count"] if "token_count" in keys else None,
        supersededBy=row["superseded_by"] if "superseded_by" in keys else None,
    )


def db_get_regenerate_history(conversation_id: str) -> list[MessageItem]:
    """
    用于重新回答：
    - 如果最后一条是 assistant，先在外部删除
    - 返回完整历史，此时最后一条通常是 user
    """
    return db_get_messages(conversation_id)


def db_get_messages(conversation_id: str) -> list[MessageItem]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content, file_name, image_preview, file_context, model, provider_name, token_count, superseded_by FROM messages WHERE conversation_id=? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall()
    conn.close()

    return [message_item_from_row(row) for row in rows]


def db_delete_message_and_after_raw(conversation_id: str, message_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM messages WHERE conversation_id=? AND id>=?",
        (conversation_id, message_id),
    )
    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?",
        (now_ms(), conversation_id),
    )
    conn.commit()
    conn.close()


def db_get_message_by_id(conversation_id: str, message_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id=? AND id=?",
        (conversation_id, message_id),
    ).fetchone()
    conn.close()
    return row


def db_get_messages_before_id(conversation_id: str, message_id: int) -> list[MessageItem]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, role, content, file_name, image_preview, file_context, model, provider_name, token_count, superseded_by FROM messages WHERE conversation_id=? AND id<? ORDER BY id ASC",
        (conversation_id, message_id),
    ).fetchall()
    conn.close()

    return [message_item_from_row(row) for row in rows]


def db_mark_message_superseded(message_id: int, superseded_by: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT conversation_id FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE messages SET superseded_by=? WHERE id=?",
            (superseded_by, message_id),
        )
        conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now_ms(), row["conversation_id"]),
        )
    conn.commit()
    conn.close()


def db_get_message_superseded_by(message_id: int):
    conn = get_conn()
    row = conn.execute(
        "SELECT superseded_by FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()
    conn.close()
    if row:
        return row["superseded_by"]
    return None
