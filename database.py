import sqlite3
import time
from config import DB_NAME

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # جدول المستخدمين
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        username TEXT,
        messages_count INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        is_restricted INTEGER DEFAULT 0,
        gender TEXT,
        start_notified INTEGER DEFAULT 0
    )''')
    # ترحيل قواعد البيانات الموجودة قبل إضافة اختيار الجنس.
    c.execute("PRAGMA table_info(users)")
    user_columns = {row[1] for row in c.fetchall()}
    if "gender" not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN gender TEXT")
    if "start_notified" not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN start_notified INTEGER DEFAULT 0")
    # ربط رسائل المشرف بالمستخدم حتى تعمل الردود على رسالة المحتوى أيضاً.
    c.execute('''CREATE TABLE IF NOT EXISTS message_targets (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    )''')
    # بيانات اتصال Telegram Business وصندوق أحداث الرسائل.
    c.execute('''CREATE TABLE IF NOT EXISTS business_connections (
        connection_id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        is_enabled INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS business_message_metadata (
        connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        sender_id INTEGER,
        sender_name TEXT NOT NULL DEFAULT '',
        sender_username TEXT,
        is_incoming INTEGER NOT NULL DEFAULT 1,
        message_text TEXT NOT NULL DEFAULT '',
        media_signature TEXT NOT NULL DEFAULT '',
        recorded_at INTEGER NOT NULL,
        PRIMARY KEY (connection_id, chat_id, message_id)
    )''')
    c.execute("PRAGMA table_info(business_message_metadata)")
    business_message_columns = {row[1] for row in c.fetchall()}
    if "message_text" not in business_message_columns:
        c.execute(
            "ALTER TABLE business_message_metadata "
            "ADD COLUMN message_text TEXT NOT NULL DEFAULT ''"
        )
    if "media_signature" not in business_message_columns:
        c.execute(
            "ALTER TABLE business_message_metadata "
            "ADD COLUMN media_signature TEXT NOT NULL DEFAULT ''"
        )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_business_message_recorded_at "
        "ON business_message_metadata(recorded_at)"
    )
    c.execute('''CREATE TABLE IF NOT EXISTS business_media_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        file_path TEXT NOT NULL UNIQUE,
        file_name TEXT,
        mime_type TEXT,
        file_size INTEGER,
        recorded_at INTEGER NOT NULL
    )''')
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_business_media_recorded_at "
        "ON business_media_files(recorded_at)"
    )
    # جدول الإعدادات
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    # القيم الافتراضية
    defaults = {
        "welcome_message": "مرحبا بك في سايت عمو محمد",
        "ack_message": "تم ارسال رسالتك انتظر يا حلو",
        "force_channel": "",
        "force_bot": "",
        "broadcast_text": ""
    }
    for key, value in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def add_user(user_id, full_name, username):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?, ?, ?)", (user_id, full_name, username))
    c.execute("UPDATE users SET full_name = ?, username = ? WHERE user_id = ?", (full_name, username, user_id))
    conn.commit()
    conn.close()

def mark_user_start_notified(user_id):
    """يرجع True مرة واحدة فقط عند أول أمر /start لهذا المستخدم."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET start_notified = 1 WHERE user_id = ? AND start_notified = 0",
        (user_id,),
    )
    is_first_start = c.rowcount == 1
    conn.commit()
    conn.close()
    return is_first_start

def increment_message_count(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET messages_count = messages_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def get_user_gender(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_gender(user_id, gender):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET gender = ? WHERE user_id = ?", (gender, user_id))
    conn.commit()
    conn.close()

def save_message_target(chat_id, message_id, user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO message_targets (chat_id, message_id, user_id) VALUES (?, ?, ?)",
        (chat_id, message_id, user_id),
    )
    conn.commit()
    conn.close()

def get_message_target(chat_id, message_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT user_id FROM message_targets WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_status(user_id, status_type, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    col = "is_banned" if status_type == "ban" else "is_restricted"
    c.execute(f"UPDATE users SET {col} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    return [u[0] for u in users]


# Telegram Business connection state and minimal message metadata.
def save_business_connection(connection_id, owner_id, is_enabled):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO business_connections "
        "(connection_id, owner_id, is_enabled, updated_at) VALUES (?, ?, ?, ?)",
        (connection_id, owner_id, int(bool(is_enabled)), int(time.time())),
    )
    conn.commit()
    conn.close()


def get_business_connection(connection_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT owner_id, is_enabled FROM business_connections WHERE connection_id = ?",
        (connection_id,),
    )
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return {"owner_id": row[0], "is_enabled": bool(row[1])}


def count_active_business_connections(owner_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM business_connections "
        "WHERE owner_id = ? AND is_enabled = 1",
        (owner_id,),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


def save_business_message_metadata(
    connection_id,
    chat_id,
    message_id,
    sender_id,
    sender_name,
    sender_username,
    is_incoming,
    message_text="",
    media_signature="",
    recorded_at=None,
):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO business_message_metadata "
        "(connection_id, chat_id, message_id, sender_id, sender_name, "
        "sender_username, is_incoming, message_text, media_signature, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            connection_id,
            chat_id,
            message_id,
            sender_id,
            sender_name or "",
            sender_username,
            int(bool(is_incoming)),
            message_text or "",
            media_signature or "",
            int(recorded_at if recorded_at is not None else time.time()),
        ),
    )
    conn.commit()
    conn.close()


def get_business_message_metadata(connection_id, chat_id, message_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT sender_id, sender_name, sender_username, is_incoming, "
        "message_text, media_signature "
        "FROM business_message_metadata "
        "WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
        (connection_id, chat_id, message_id),
    )
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "sender_id": row[0],
        "sender_name": row[1],
        "sender_username": row[2],
        "is_incoming": bool(row[3]),
        "message_text": row[4],
        "media_signature": row[5],
    }


def add_business_media_file(
    connection_id,
    chat_id,
    message_id,
    file_path,
    file_name=None,
    mime_type=None,
    file_size=None,
    recorded_at=None,
):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO business_media_files "
        "(connection_id, chat_id, message_id, file_path, file_name, "
        "mime_type, file_size, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            connection_id,
            chat_id,
            message_id,
            file_path,
            file_name,
            mime_type,
            file_size,
            int(recorded_at if recorded_at is not None else time.time()),
        ),
    )
    conn.commit()
    conn.close()


def get_expired_business_media_files(retention_days):
    cutoff = int(time.time()) - max(1, int(retention_days)) * 86400
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT file_path FROM business_media_files WHERE recorded_at < ?",
        (cutoff,),
    )
    paths = [row[0] for row in c.fetchall()]
    conn.close()
    return paths


def delete_business_media_file(file_path):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM business_media_files WHERE file_path = ?", (file_path,))
    conn.commit()
    conn.close()


def delete_business_message_metadata(connection_id, chat_id, message_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "DELETE FROM business_message_metadata "
        "WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
        (connection_id, chat_id, message_id),
    )
    conn.commit()
    conn.close()


def delete_business_message_metadata_for_connection(connection_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "DELETE FROM business_message_metadata WHERE connection_id = ?",
        (connection_id,),
    )
    conn.commit()
    conn.close()


def prune_business_message_metadata(retention_days):
    cutoff = int(time.time()) - max(1, int(retention_days)) * 86400
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "DELETE FROM business_message_metadata WHERE recorded_at < ?",
        (cutoff,),
    )
    conn.commit()
    conn.close()
