import os
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Optional

from config import DEFAULT_MODEL
from schemas import ApiProfileBody, MessageItem, SystemPromptBody

BASE_DIR = Path(os.environ.get("CLAUDE_WEB_BASE_DIR") or Path(__file__).resolve().parent)
DB_PATH = Path(os.environ.get("CLAUDE_WEB_DB_PATH") or (BASE_DIR / "chat_multi.db"))
SINGLE_USER_ID = os.environ.get("CLAUDE_WEB_SINGLE_USER_ID", "").strip()
SINGLE_USERNAME = os.environ.get("CLAUDE_WEB_SINGLE_USERNAME", "local").strip() or "local"
SINGLE_EMAIL = os.environ.get("CLAUDE_WEB_SINGLE_EMAIL", "").strip()
SINGLE_PASSWORD_HASH = os.environ.get("CLAUDE_WEB_SINGLE_PASSWORD_HASH", "!").strip() or "!"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def add_column_if_missing(cur, table: str, column: str, definition: str):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass


def _pick_single_user_row(cur):
    if SINGLE_USER_ID:
        row = cur.execute(
            "SELECT id, username, email, password_hash, created_at FROM users WHERE id=?",
            (SINGLE_USER_ID,),
        ).fetchone()
        if row:
            return row
    row = cur.execute(
        """
        SELECT users.id, users.username, users.email, users.password_hash, users.created_at
        FROM users
        LEFT JOIN conversations ON conversations.user_id = users.id
        GROUP BY users.id
        ORDER BY COUNT(conversations.id) DESC, users.created_at ASC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return row
    return None


def ensure_single_user_data(cur):
    row = _pick_single_user_row(cur)
    if row:
        single_user_id = row[0]
    else:
        single_user_id = SINGLE_USER_ID or new_id()
        cur.execute(
            "INSERT INTO users (id, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (single_user_id, SINGLE_USERNAME, SINGLE_EMAIL, SINGLE_PASSWORD_HASH, now_ms()),
        )

    cur.execute("UPDATE conversations SET user_id=? WHERE user_id IS NULL OR user_id<>?", (single_user_id, single_user_id))
    cur.execute("UPDATE api_profiles SET user_id=? WHERE user_id IS NULL OR user_id<>?", (single_user_id, single_user_id))
    cur.execute("UPDATE system_prompts SET user_id=? WHERE user_id IS NULL OR user_id<>?", (single_user_id, single_user_id))
    return single_user_id


def db_get_single_user_auth():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    single_user_id = ensure_single_user_data(cur)
    conn.commit()
    row = cur.execute(
        "SELECT id, username, email, password_hash, created_at FROM users WHERE id=?",
        (single_user_id,),
    ).fetchone()
    conn.close()
    return row


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        email TEXT,
        password_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS email_verification_codes (
        email TEXT PRIMARY KEY,
        purpose TEXT NOT NULL DEFAULT 'register',
        code TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS auth_sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        user_id TEXT,
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
        ("users", "email", "TEXT"),
        ("email_verification_codes", "purpose", "TEXT NOT NULL DEFAULT 'register'"),
        ("conversations", "user_id", "TEXT"),
        ("messages", "model", "TEXT"),
        ("messages", "provider_name", "TEXT"),
        ("messages", "token_count", "INTEGER"),
        ("messages", "file_context", "TEXT"),
        ("messages", "superseded_by", "INTEGER"),
        ("messages", "sources", "TEXT"),
        ("conversations", "is_pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("api_profiles", "user_id", "TEXT"),
        ("api_profiles", "protocol", "TEXT"),
        ("system_prompts", "user_id", "TEXT"),
    ]:
        add_column_if_missing(cur, table, column, definition)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_email_verification_expires ON email_verification_codes(expires_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS api_profiles (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        auth_token TEXT NOT NULL,
        model TEXT NOT NULL,
        protocol TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_profiles_user_updated ON api_profiles(user_id, updated_at)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_prompts (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_system_prompts_user_updated ON system_prompts(user_id, updated_at)")

    ensure_single_user_data(cur)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"DB created: {DB_PATH}")


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    return uuid.uuid4().hex


def db_create_user(username: str, email: str, password_hash: str) -> str:
    uid = new_id()
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (uid, username, email, password_hash, now_ms()),
    )
    conn.commit()
    conn.close()
    return uid


def db_get_user_by_username(username: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, email, password_hash, created_at FROM users WHERE lower(username)=lower(?)",
        (username,),
    ).fetchone()
    conn.close()
    return row


def db_get_user_by_email(email: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, email, password_hash, created_at FROM users WHERE lower(email)=lower(?)",
        (email,),
    ).fetchone()
    conn.close()
    return row


def db_get_user_by_id(user_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, email, created_at FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def db_get_user_auth_by_id(user_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT id, username, email, password_hash, created_at FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def db_update_user_profile(user_id: str, username: str, email: str):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET username=?, email=? WHERE id=?",
        (username, email, user_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, username, email, created_at FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def db_update_user_password_by_id(user_id: str, password_hash: str):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (password_hash, user_id),
    )
    conn.commit()
    conn.close()


def db_create_session(token: str, user_id: str, expires_at: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO auth_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now_ms(), expires_at),
    )
    conn.commit()
    conn.close()


def db_get_session_user(token: str):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT users.id, users.username, users.email, users.created_at
        FROM auth_sessions
        JOIN users ON users.id = auth_sessions.user_id
        WHERE auth_sessions.token=? AND auth_sessions.expires_at>?
        """,
        (token, now_ms()),
    ).fetchone()
    conn.close()
    return row


def db_delete_session(token: str):
    conn = get_conn()
    conn.execute("DELETE FROM auth_sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


def db_delete_other_sessions_by_user(user_id: str, keep_token: str):
    conn = get_conn()
    conn.execute("DELETE FROM auth_sessions WHERE user_id=? AND token<>?", (user_id, keep_token))
    conn.commit()
    conn.close()


def db_save_email_verification_code(email: str, purpose: str, code: str, expires_at: int):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO email_verification_codes (email, purpose, code, created_at, expires_at, consumed_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        ON CONFLICT(email) DO UPDATE SET
            purpose=excluded.purpose,
            code=excluded.code,
            created_at=excluded.created_at,
            expires_at=excluded.expires_at,
            consumed_at=NULL
        """,
        (email, purpose, code, now_ms(), expires_at),
    )
    conn.commit()
    conn.close()


def db_get_email_verification_code(email: str, purpose: str = ""):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT email, purpose, code, created_at, expires_at, consumed_at
        FROM email_verification_codes
        WHERE lower(email)=lower(?) AND (? = '' OR purpose=?)
        """,
        (email, purpose, purpose),
    ).fetchone()
    conn.close()
    return row


def db_verify_email_code(email: str, purpose: str, code: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT code FROM email_verification_codes
        WHERE lower(email)=lower(?) AND purpose=? AND expires_at>? AND consumed_at IS NULL
        """,
        (email, purpose, now_ms()),
    ).fetchone()
    if not row or row["code"] != code:
        conn.close()
        return False
    conn.execute(
        "UPDATE email_verification_codes SET consumed_at=? WHERE lower(email)=lower(?) AND purpose=?",
        (now_ms(), email, purpose),
    )
    conn.commit()
    conn.close()
    return True


def db_update_user_password_by_email(email: str, password_hash: str):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_hash=? WHERE lower(email)=lower(?)",
        (password_hash, email),
    )
    conn.commit()
    conn.close()


def db_delete_sessions_by_user(user_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def db_user_owns_conversation(user_id: str, conversation_id: str) -> bool:
    if not user_id or not conversation_id:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM conversations WHERE id=? AND user_id=?",
        (conversation_id, user_id),
    ).fetchone()
    conn.close()
    return bool(row)


def db_create_conversation(title: str = "新对话", user_id: str = "") -> str:
    cid = new_id()
    ts = now_ms()
    conn = get_conn()
    conn.execute(
        "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (cid, user_id, title or "新对话", ts, ts),
    )
    conn.commit()
    conn.close()
    return cid


def db_ensure_conversation(cid: str, title: str = "新对话", user_id: str = "") -> str:
    if cid:
        conn = get_conn()
        row = conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (cid, user_id)).fetchone()
        if row:
            conn.close()
            return cid
        conn.close()

    return db_create_conversation(title, user_id=user_id)


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
    sources: Optional[str] = None,
):
    ts = now_ms()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO messages (conversation_id, role, content, file_name, image_preview, file_context, model, provider_name, token_count, superseded_by, sources, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (conversation_id, role, content or "", file_name, image_preview, file_context, model, provider_name, token_count, superseded_by, sources, ts),
    )
    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id=?",
        (ts, conversation_id),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def db_update_title_if_needed(conversation_id: str, title_source: str, user_id: str = ""):
    conn = get_conn()
    row = conn.execute("SELECT title FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
    if row and (row["title"].startswith("新对话") or row["title"].strip() == ""):
        title = (title_source or "新对话").strip().replace("\n", " ")[:18] or "新对话"
        conn.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (title, now_ms(), conversation_id, user_id),
        )
        conn.commit()
    conn.close()


def db_list_api_profiles(user_id: str = ""):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, base_url, auth_token, model, protocol, is_default, created_at, updated_at FROM api_profiles WHERE user_id=? ORDER BY is_default DESC, updated_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def db_save_api_profile(profile_id: str, body: ApiProfileBody, user_id: str = ""):
    ts = now_ms()
    pid = profile_id or new_id()

    conn = get_conn()

    if body.is_default:
        conn.execute("UPDATE api_profiles SET is_default=0 WHERE user_id=?", (user_id,))

    old = conn.execute("SELECT id FROM api_profiles WHERE id=? AND user_id=?", (pid, user_id)).fetchone()

    if old:
        conn.execute(
            """
            UPDATE api_profiles
            SET name=?, base_url=?, auth_token=?, model=?, protocol=?, is_default=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (
                body.name.strip() or "未命名接入商",
                body.base_url.strip(),
                body.auth_token.strip(),
                body.model.strip() or DEFAULT_MODEL,
                (body.protocol or "").strip(),
                1 if body.is_default else 0,
                ts,
                pid,
                user_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO api_profiles
            (id, user_id, name, base_url, auth_token, model, protocol, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                user_id,
                body.name.strip() or "未命名接入商",
                body.base_url.strip(),
                body.auth_token.strip(),
                body.model.strip() or DEFAULT_MODEL,
                (body.protocol or "").strip(),
                1 if body.is_default else 0,
                ts,
                ts,
            ),
        )

    conn.commit()
    conn.close()
    return pid


def db_delete_api_profile(profile_id: str, user_id: str = ""):
    conn = get_conn()
    conn.execute("DELETE FROM api_profiles WHERE id=? AND user_id=?", (profile_id, user_id))
    conn.commit()
    conn.close()


def db_set_default_api_profile(profile_id: str, user_id: str = ""):
    conn = get_conn()
    conn.execute("UPDATE api_profiles SET is_default=0 WHERE user_id=?", (user_id,))
    conn.execute(
        "UPDATE api_profiles SET is_default=1, updated_at=? WHERE id=? AND user_id=?",
        (now_ms(), profile_id, user_id),
    )
    conn.commit()
    conn.close()


def db_list_system_prompts(user_id: str = ""):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, content, enabled, created_at, updated_at FROM system_prompts WHERE user_id=? ORDER BY enabled DESC, updated_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def db_save_system_prompt(prompt_id: str, body: SystemPromptBody, user_id: str = ""):
    ts = now_ms()
    pid = prompt_id or new_id()
    title = (body.title or "").strip() or "系统提示词"
    content = (body.content or "").strip()

    conn = get_conn()
    old = conn.execute("SELECT id FROM system_prompts WHERE id=? AND user_id=?", (pid, user_id)).fetchone()

    if old:
        conn.execute(
            """
            UPDATE system_prompts
            SET title=?, content=?, enabled=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (title, content, 1 if body.enabled else 0, ts, pid, user_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO system_prompts
            (id, user_id, title, content, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (pid, user_id, title, content, 1 if body.enabled else 0, ts, ts),
        )

    conn.commit()
    conn.close()
    return pid


def db_delete_system_prompt(prompt_id: str, user_id: str = ""):
    conn = get_conn()
    conn.execute("DELETE FROM system_prompts WHERE id=? AND user_id=?", (prompt_id, user_id))
    conn.commit()
    conn.close()


def db_set_system_prompt_enabled(prompt_id: str, enabled: bool, user_id: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    ts = now_ms()
    if enabled:
        cur.execute(
            "UPDATE system_prompts SET enabled=0, updated_at=? WHERE user_id=? AND id<>? AND enabled<>0",
            (ts, user_id, prompt_id),
        )
    cur.execute(
        "UPDATE system_prompts SET enabled=?, updated_at=? WHERE id=? AND user_id=?",
        (1 if enabled else 0, ts, prompt_id, user_id),
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
        sources=row["sources"] if "sources" in keys else None,
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
        "SELECT id, role, content, file_name, image_preview, file_context, model, provider_name, token_count, superseded_by, sources FROM messages WHERE conversation_id=? ORDER BY id ASC",
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
        "SELECT id, role, content, file_name, image_preview, file_context, model, provider_name, token_count, superseded_by, sources FROM messages WHERE conversation_id=? AND id<? ORDER BY id ASC",
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
