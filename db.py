from pathlib import Path
import sqlite3

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
