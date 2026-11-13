import sqlite3
import time
from config import DB_NAME, TOKEN_ENC_KEY

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
    c.execute("PRAGMA table_info(business_media_files)")
    business_media_columns = {row[1] for row in c.fetchall()}
    if "media_type" not in business_media_columns:
        c.execute("ALTER TABLE business_media_files ADD COLUMN media_type TEXT")
    if "file_id" not in business_media_columns:
        c.execute("ALTER TABLE business_media_files ADD COLUMN file_id TEXT")
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_business_media_recorded_at "
        "ON business_media_files(recorded_at)"
    )
    # --- قسم «قم بإنشاء بوت»: البوتات المصنوعة من السايت ---
    c.execute('''CREATE TABLE IF NOT EXISTS clone_bots (
        bot_id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE,
        username TEXT,
        name TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    )''')
    # بوت واحد لكل عضو.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clone_bots_owner ON clone_bots(owner_id)")
    c.execute('''CREATE TABLE IF NOT EXISTS clone_users (
        bot_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        full_name TEXT,
        username TEXT,
        messages_count INTEGER NOT NULL DEFAULT 0,
        is_banned INTEGER NOT NULL DEFAULT 0,
        gender TEXT,
        start_notified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bot_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS clone_settings (
        bot_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (bot_id, key)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS clone_message_targets (
        bot_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (bot_id, chat_id, message_id)
    )''')
    # --- الاشتراك الإجباري: scope = main (بوت المالك فقط) | global (كل البوتات) | clone (بوت صانع) ---
    c.execute('''CREATE TABLE IF NOT EXISTS force_subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        bot_id INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        username TEXT,
        title TEXT,
        link TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE (scope, bot_id, chat_id)
    )''')
    # القنوات والجروبات التي أُضيف إليها كل بوت (للإذاعة). bot_id = 0 للبوت الرئيسي.
    c.execute('''CREATE TABLE IF NOT EXISTS known_chats (
        bot_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL,
        title TEXT,
        can_post INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (bot_id, chat_id)
    )''')
    # --- مركز التحكم: حظر عام، سجل نشاط، إحالات ---
    c.execute('''CREATE TABLE IF NOT EXISTS global_bans (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        created_at INTEGER NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        actor INTEGER,
        bot_id INTEGER NOT NULL DEFAULT 0,
        action TEXT NOT NULL,
        detail TEXT
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts)")
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        user_id INTEGER PRIMARY KEY,
        referrer_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
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
    migrate_tokens()

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
    media_type=None,
    file_id=None,
):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO business_media_files "
        "(connection_id, chat_id, message_id, file_path, file_name, "
        "mime_type, file_size, recorded_at, media_type, file_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            connection_id,
            chat_id,
            message_id,
            file_path,
            file_name,
            mime_type,
            file_size,
            int(recorded_at if recorded_at is not None else time.time()),
            media_type,
            file_id,
        ),
    )
    conn.commit()
    conn.close()


def get_business_media_files(connection_id, chat_id, message_id):
    """Return archived attachments saved for one Business message."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT file_path, file_name, mime_type, media_type, file_id "
        "FROM business_media_files "
        "WHERE connection_id = ? AND chat_id = ? AND message_id = ? "
        "ORDER BY id",
        (connection_id, chat_id, message_id),
    )
    rows = c.fetchall()
    conn.close()
    return [
        {
            "file_path": row[0],
            "file_name": row[1],
            "mime_type": row[2],
            "media_type": row[3],
            "file_id": row[4],
        }
        for row in rows
    ]


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


# ---------------------------------------------------------------------------
# البوتات المصنوعة (قسم «قم بإنشاء بوت»)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# تشفير توكنات البوتات المصنوعة (اختياري: يعمل عند وضع TOKEN_ENC_KEY في .env)
# ---------------------------------------------------------------------------
_ENC_PREFIX = "enc:"
_FERNET = None


def _fernet():
    global _FERNET
    if _FERNET is None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise RuntimeError(
                "TOKEN_ENC_KEY مضبوط لكن مكتبة cryptography غير مثبتة. نفّذ: pip install cryptography"
            ) from exc
        _FERNET = Fernet(TOKEN_ENC_KEY.encode())
    return _FERNET


def encrypt_token(token):
    if not TOKEN_ENC_KEY or token.startswith(_ENC_PREFIX):
        return token
    return _ENC_PREFIX + _fernet().encrypt(token.encode()).decode()


def decrypt_token(stored):
    if not stored.startswith(_ENC_PREFIX):
        return stored
    if not TOKEN_ENC_KEY:
        raise RuntimeError("توكنات البوتات مشفرة لكن TOKEN_ENC_KEY غير موجود في .env")
    return _fernet().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()


def migrate_tokens():
    """يشفّر التوكنات القديمة المحفوظة نصاً عادياً (مرة واحدة، فقط إن وُجد المفتاح)."""
    if not TOKEN_ENC_KEY:
        return
    conn = sqlite3.connect(DB_NAME)
    try:
        rows = conn.execute("SELECT bot_id, token FROM clone_bots").fetchall()
        for bot_id, token in rows:
            if not token.startswith(_ENC_PREFIX):
                conn.execute(
                    "UPDATE clone_bots SET token = ? WHERE bot_id = ?",
                    (encrypt_token(token), bot_id),
                )
        conn.commit()
    finally:
        conn.close()


def _clone_row(row):
    if row is None:
        return None
    return {
        "bot_id": row[0],
        "owner_id": row[1],
        "token": decrypt_token(row[2]),
        "username": row[3],
        "name": row[4],
        "is_active": bool(row[5]),
        "created_at": row[6],
    }


_CLONE_COLS = "bot_id, owner_id, token, username, name, is_active, created_at"


def save_clone(bot_id, owner_id, token, username, name):
    """يضيف بوتاً جديداً أو يحدّث توكن بوت موجود مع الإبقاء على أعضائه وإعداداته."""
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            "INSERT INTO clone_bots "
            "(bot_id, owner_id, token, username, name, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(bot_id) DO UPDATE SET token = excluded.token, "
            "username = excluded.username, name = excluded.name, is_active = 1",
            (bot_id, owner_id, encrypt_token(token), username, name, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_clone(bot_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"SELECT {_CLONE_COLS} FROM clone_bots WHERE bot_id = ?", (bot_id,))
    row = c.fetchone()
    conn.close()
    return _clone_row(row)


def get_clone_by_owner(owner_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"SELECT {_CLONE_COLS} FROM clone_bots WHERE owner_id = ?", (owner_id,))
    row = c.fetchone()
    conn.close()
    return _clone_row(row)


def list_clones(active_only=False):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    query = f"SELECT {_CLONE_COLS} FROM clone_bots"
    if active_only:
        query += " WHERE is_active = 1"
    c.execute(query + " ORDER BY created_at")
    rows = c.fetchall()
    conn.close()
    return [_clone_row(r) for r in rows]


def set_clone_active(bot_id, is_active):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE clone_bots SET is_active = ? WHERE bot_id = ?",
        (int(bool(is_active)), bot_id),
    )
    conn.commit()
    conn.close()


def delete_clone(bot_id):
    """يحذف البوت وكل أعضائه وإعداداته وروابط رسائله."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for table in ("clone_users", "clone_settings", "clone_message_targets",
                  "force_subs", "known_chats", "clone_bots"):
        c.execute(f"DELETE FROM {table} WHERE bot_id = ?", (bot_id,))
    conn.commit()
    conn.close()


def clone_get_setting(bot_id, key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT value FROM clone_settings WHERE bot_id = ? AND key = ?",
        (bot_id, key),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def clone_set_setting(bot_id, key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO clone_settings (bot_id, key, value) VALUES (?, ?, ?)",
        (bot_id, key, value),
    )
    conn.commit()
    conn.close()


def clone_del_setting(bot_id, *keys):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for key in keys:
        c.execute(
            "DELETE FROM clone_settings WHERE bot_id = ? AND key = ?",
            (bot_id, key),
        )
    conn.commit()
    conn.close()


def clone_add_user(bot_id, user_id, full_name, username):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO clone_users (bot_id, user_id, full_name, username) "
        "VALUES (?, ?, ?, ?)",
        (bot_id, user_id, full_name, username),
    )
    c.execute(
        "UPDATE clone_users SET full_name = ?, username = ? "
        "WHERE bot_id = ? AND user_id = ?",
        (full_name, username, bot_id, user_id),
    )
    conn.commit()
    conn.close()


def clone_ensure_user(bot_id, user_id):
    """ينشئ سجلاً فارغاً لعضو غير معروف (للحظر المسبق بالأيدي) دون مسح بياناته."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO clone_users (bot_id, user_id, full_name) VALUES (?, ?, '')",
        (bot_id, user_id),
    )
    conn.commit()
    conn.close()


def clone_get_user(bot_id, user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT user_id, full_name, username, messages_count, is_banned, gender, "
        "start_notified FROM clone_users WHERE bot_id = ? AND user_id = ?",
        (bot_id, user_id),
    )
    row = c.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "user_id": row[0],
        "full_name": row[1],
        "username": row[2],
        "messages_count": row[3],
        "is_banned": bool(row[4]),
        "gender": row[5],
        "start_notified": bool(row[6]),
    }


def clone_find_user_by_username(bot_id, username):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT user_id FROM clone_users "
        "WHERE bot_id = ? AND LOWER(username) = LOWER(?)",
        (bot_id, (username or "").lstrip("@")),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def clone_mark_start_notified(bot_id, user_id):
    """يرجع True مرة واحدة فقط عند أول /start لهذا العضو في هذا البوت."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE clone_users SET start_notified = 1 "
        "WHERE bot_id = ? AND user_id = ? AND start_notified = 0",
        (bot_id, user_id),
    )
    first = c.rowcount == 1
    conn.commit()
    conn.close()
    return first


def clone_increment_messages(bot_id, user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE clone_users SET messages_count = messages_count + 1 "
        "WHERE bot_id = ? AND user_id = ?",
        (bot_id, user_id),
    )
    c.execute(
        "SELECT messages_count FROM clone_users WHERE bot_id = ? AND user_id = ?",
        (bot_id, user_id),
    )
    row = c.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0


def clone_set_gender(bot_id, user_id, gender):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE clone_users SET gender = ? WHERE bot_id = ? AND user_id = ?",
        (gender, bot_id, user_id),
    )
    conn.commit()
    conn.close()


def clone_set_ban(bot_id, user_id, banned):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "UPDATE clone_users SET is_banned = ? WHERE bot_id = ? AND user_id = ?",
        (int(bool(banned)), bot_id, user_id),
    )
    conn.commit()
    conn.close()


def clone_user_ids(bot_id, exclude_user_id=None):
    """أعضاء البوت غير المحظورين (للإذاعة)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT user_id FROM clone_users WHERE bot_id = ? AND is_banned = 0",
        (bot_id,),
    )
    ids = [row[0] for row in c.fetchall() if row[0] != exclude_user_id]
    conn.close()
    return ids


def clone_stats(bot_id):
    """(عدد الأعضاء، عدد المحظورين، مجموع الرسائل)"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_banned), 0), COALESCE(SUM(messages_count), 0) "
        "FROM clone_users WHERE bot_id = ?",
        (bot_id,),
    )
    row = c.fetchone()
    conn.close()
    return row[0], row[1], row[2]


def clone_save_target(bot_id, chat_id, message_id, user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO clone_message_targets "
        "(bot_id, chat_id, message_id, user_id) VALUES (?, ?, ?, ?)",
        (bot_id, chat_id, message_id, user_id),
    )
    conn.commit()
    conn.close()


def clone_get_target(bot_id, chat_id, message_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(
        "SELECT user_id FROM clone_message_targets "
        "WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
        (bot_id, chat_id, message_id),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None



# ---------------------------------------------------------------------------
# الاشتراك الإجباري
# ---------------------------------------------------------------------------
_FS_COLS = "id, scope, bot_id, kind, chat_id, username, title, link"


def _fs_row(r):
    return {
        "id": r[0], "scope": r[1], "bot_id": r[2], "kind": r[3],
        "chat_id": r[4], "username": r[5], "title": r[6], "link": r[7],
    }


def add_force_sub(scope, bot_id, kind, chat_id, username, title, link):
    """يضيف اشتراكاً إجبارياً. يرجع False إن كان موجوداً مسبقاً."""
    conn = sqlite3.connect(DB_NAME)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO force_subs "
            "(scope, bot_id, kind, chat_id, username, title, link, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, bot_id, kind, chat_id, username, title, link, int(time.time())),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_force_subs(scopes, bot_id=0):
    """scopes: قائمة من main/global/clone. bot_id يخص scope=clone فقط."""
    conn = sqlite3.connect(DB_NAME)
    try:
        marks = ",".join("?" for _ in scopes)
        rows = conn.execute(
            f"SELECT {_FS_COLS} FROM force_subs "
            f"WHERE scope IN ({marks}) AND (scope != 'clone' OR bot_id = ?) "
            "ORDER BY id",
            (*scopes, bot_id),
        ).fetchall()
    finally:
        conn.close()
    return [_fs_row(r) for r in rows]


def get_force_sub(sub_id):
    conn = sqlite3.connect(DB_NAME)
    try:
        row = conn.execute(
            f"SELECT {_FS_COLS} FROM force_subs WHERE id = ?", (sub_id,)
        ).fetchone()
    finally:
        conn.close()
    return _fs_row(row) if row else None


def delete_force_sub(sub_id):
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute("DELETE FROM force_subs WHERE id = ?", (sub_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# القنوات والجروبات المعروفة لكل بوت (للإذاعة)
# ---------------------------------------------------------------------------
def upsert_known_chat(bot_id, chat_id, chat_type, title, can_post):
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            "INSERT INTO known_chats (bot_id, chat_id, chat_type, title, can_post) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, chat_id) DO UPDATE SET chat_type = excluded.chat_type, "
            "title = excluded.title, can_post = excluded.can_post",
            (bot_id, chat_id, chat_type, title, int(bool(can_post))),
        )
        conn.commit()
    finally:
        conn.close()


def delete_known_chat(bot_id, chat_id):
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            "DELETE FROM known_chats WHERE bot_id = ? AND chat_id = ?", (bot_id, chat_id)
        )
        conn.commit()
    finally:
        conn.close()


def list_known_chats(bot_id, postable_only=True):
    conn = sqlite3.connect(DB_NAME)
    try:
        query = "SELECT chat_id FROM known_chats WHERE bot_id = ?"
        if postable_only:
            query += " AND can_post = 1"
        return [r[0] for r in conn.execute(query, (bot_id,)).fetchall()]
    finally:
        conn.close()


def ensure_known_chat(bot_id, chat_id, chat_type, title):
    """يسجل القناة/الجروب إن لم يكن مسجلاً، ولا يغيّر صلاحية الإرسال المسجلة."""
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO known_chats (bot_id, chat_id, chat_type, title, can_post) "
            "VALUES (?, ?, ?, ?, 1)",
            (bot_id, chat_id, chat_type, title),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# الحظر العام (البوت الرئيسي + كل البوتات المصنوعة)
# ---------------------------------------------------------------------------
_GBANS = None


def _gban_cache():
    global _GBANS
    if _GBANS is None:
        conn = sqlite3.connect(DB_NAME)
        try:
            _GBANS = {r[0] for r in conn.execute("SELECT user_id FROM global_bans")}
        finally:
            conn.close()
    return _GBANS


def is_globally_banned(user_id):
    return user_id in _gban_cache()


def add_global_ban(user_id, reason=""):
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO global_bans (user_id, reason, created_at) VALUES (?, ?, ?)",
            (user_id, reason, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    _gban_cache().add(user_id)


def remove_global_ban(user_id):
    conn = sqlite3.connect(DB_NAME)
    try:
        cur = conn.execute("DELETE FROM global_bans WHERE user_id = ?", (user_id,))
        conn.commit()
        removed = cur.rowcount > 0
    finally:
        conn.close()
    _gban_cache().discard(user_id)
    return removed


def count_global_bans():
    return len(_gban_cache())


def find_user_id_by_username(username):
    """يبحث عن يوزر بين أعضاء السايت ثم أعضاء كل البوتات المصنوعة."""
    name = username.lstrip("@").lower()
    conn = sqlite3.connect(DB_NAME)
    try:
        for table in ("users", "clone_users"):
            row = conn.execute(
                f"SELECT user_id FROM {table} WHERE LOWER(username) = ? LIMIT 1", (name,)
            ).fetchone()
            if row:
                return row[0]
    finally:
        conn.close()
    return None


# ---------------------------------------------------------------------------
# سجل النشاط
# ---------------------------------------------------------------------------
def log_activity(action, actor=None, bot_id=0, detail=""):
    try:
        conn = sqlite3.connect(DB_NAME)
        try:
            conn.execute(
                "INSERT INTO activity_log (ts, actor, bot_id, action, detail) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), actor, bot_id, action, detail[:300]),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # السجل لا يجب أن يعطّل أي عملية


def recent_activity(limit=20):
    conn = sqlite3.connect(DB_NAME)
    try:
        return conn.execute(
            "SELECT ts, actor, bot_id, action, detail FROM activity_log "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    finally:
        conn.close()


def prune_activity(days=60):
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute("DELETE FROM activity_log WHERE ts < ?", (int(time.time()) - days * 86400,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# الإحالات
# ---------------------------------------------------------------------------
def add_referral(user_id, referrer_id):
    """يسجّل الإحالة مرة واحدة لكل عضو، ويرفض الإحالة الذاتية. يرجع True إن سُجلت."""
    if user_id == referrer_id:
        return False
    conn = sqlite3.connect(DB_NAME)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO referrals (user_id, referrer_id, created_at) VALUES (?, ?, ?)",
            (user_id, referrer_id, int(time.time())),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_referrals(referrer_id=None):
    conn = sqlite3.connect(DB_NAME)
    try:
        if referrer_id is None:
            return conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)
        ).fetchone()[0]
    finally:
        conn.close()
