import sqlite3
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
