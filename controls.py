"""مركز تحكم المالك.

يعمل فوراً (مفعّل افتراضياً):
  • حظر عام: يمنع العضو من البوت الرئيسي ومن كل البوتات المصنوعة دفعة واحدة.
  • مكافحة السبام: من يرسل رسائل كثيرة بسرعة يُتجاهل دقيقة (لا تصلك ولا تصل البوتات رسائله).
  • نسخة احتياطية مضغوطة من قاعدة البيانات تصلك في الخاص تلقائياً.
  • سجل نشاط (إنشاء/حذف بوتات، إضافة اشتراكات، حظر عام...).

موجودة لكن مطفأة افتراضياً (تشغّلها بزر عندما تحتاجها):
  • وضع الصيانة: يوقف الرد على كل الأعضاء في كل البوتات.
  • توقيع البوتات المصنوعة: سطر «صُنع بواسطة ...» يُضاف لرسائل الترحيب والإرسال.
  • الإحالة: رابط دعوة لكل عضو وعدّاد لمن دعاهم.
المالك (OWNER_ID) لا يتأثر بأي شيء من هذا.
"""
import asyncio
import gzip
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

import config
import database as db

logger = logging.getLogger(__name__)

MAIN_BOT = None   # يُضبط عند التسجيل؛ اليوزر يُقرأ منه وقت الحاجة

# اسم المفتاح -> القيمة الافتراضية
DEFAULTS = {"maint": False, "spam": True, "sig": False, "ref": False, "backup": True}
DEFAULT_MAINT_TEXT = "🛠 البوت تحت الصيانة حالياً، سنعود قريباً."
SPAM_LIMIT = 6        # عدد الرسائل
SPAM_WINDOW = 10      # خلال كم ثانية
SPAM_MUTE = 60        # مدة التجاهل بالثواني
BACKUP_STEPS = [6, 12, 24, 72, 168]   # ساعات
BACKUP_MAX_BYTES = 49 * 1024 * 1024
TZ = timezone(timedelta(hours=3))


# ---------------------------------------------------------------------------
# مفاتيح التشغيل
# ---------------------------------------------------------------------------
def on(key):
    value = db.get_setting(f"ct_{key}")
    return DEFAULTS[key] if value in (None, "") else value == "1"


def _toggle(key):
    db.set_setting(f"ct_{key}", "0" if on(key) else "1")


def _backup_hours():
    try:
        return int(db.get_setting("ct_backup_hours") or 24)
    except ValueError:
        return 24


# ---------------------------------------------------------------------------
# البوابة: حظر عام / صيانة / سبام  (تُستدعى من force_sub.gate_user لكل رسالة)
# ---------------------------------------------------------------------------
_HITS: dict = {}    # (bot_key, user_id) -> deque of timestamps
_MUTED: dict = {}   # (bot_key, user_id) -> until


def _spam_verdict(bot_key, user_id, now=None):
    """ok | mute_start | muted"""
    now = time.time() if now is None else now
    key = (bot_key, user_id)
    if _MUTED.get(key, 0) > now:
        return "muted"
    hits = _HITS.setdefault(key, deque())
    hits.append(now)
    while hits and hits[0] < now - SPAM_WINDOW:
        hits.popleft()
    if len(hits) > SPAM_LIMIT:
        _MUTED[key] = now + SPAM_MUTE
        hits.clear()
        return "mute_start"
    if len(_HITS) > 50000:   # تنظيف دوري لمنع تضخم الذاكرة
        for k in [k for k, v in _HITS.items() if not v or v[-1] < now - SPAM_WINDOW]:
            _HITS.pop(k, None)
        for k in [k for k, v in _MUTED.items() if v < now]:
            _MUTED.pop(k, None)
    return "ok"


async def guard(context, user, chat_id, bot_key):
    """True إذا يحق للمستخدم المتابعة. يرسل له الرفض عند الحاجة."""
    if user is None or user.id == config.OWNER_ID:
        return True

    async def say(text, entities=None):
        try:
            await context.bot.send_message(chat_id or user.id, text, entities=entities)
        except BadRequest:
            try:   # تنسيق غير مدعوم في هذا البوت: نرسل النص بدونه
                await context.bot.send_message(chat_id or user.id, text)
            except Exception:
                pass
        except Exception:
            pass

    if db.is_globally_banned(user.id):
        await say("🚫 أنت محظور من استخدام البوت.")
        return False
    if on("maint"):
        await say(
            db.get_setting("ct_maint_text") or DEFAULT_MAINT_TEXT,
            parse_entities(db.get_setting("ct_maint_entities"), context.bot),
        )
        return False
    if on("spam"):
        verdict = _spam_verdict(bot_key, user.id)
        if verdict == "mute_start":
            await say("⏳ أرسلت رسائل كثيرة بسرعة. انتظر دقيقة ثم حاول مجدداً.")
            return False
        if verdict == "muted":
            return False
    return True


# ---------------------------------------------------------------------------
# تنسيق النص (روابط، إيموجي مميز، ...) وحفظه
# ---------------------------------------------------------------------------
def entities_json(message):
    return json.dumps([e.to_dict() for e in (message.entities or [])], ensure_ascii=False)


def parse_entities(raw, bot):
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return [MessageEntity.de_json(e, bot) for e in data] or None if isinstance(data, list) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _u16(text):
    """طول النص بوحدات UTF-16 (هكذا يحسب تيليجرام مواضع التنسيق)."""
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# التوقيع
# ---------------------------------------------------------------------------
def _signature():
    """(النص، قائمة تنسيقه كقواميس). الافتراضي نص عادي فيه @يوزر (تيليجرام يحوله لرابط)."""
    if not on("sig"):
        return "", []
    custom = db.get_setting("ct_sig_text")
    if custom:
        try:
            ents = json.loads(db.get_setting("ct_sig_entities") or "[]")
        except json.JSONDecodeError:
            ents = []
        return custom, ents if isinstance(ents, list) else []
    try:
        return f"🤖 صُنع بواسطة @{MAIN_BOT.username}", []
    except Exception:
        return "", []


def apply_signature(text, entities, bot):
    """يلصق التوقيع في آخر الرسالة مع إزاحة مواضع تنسيقه ودمجه مع تنسيق الرسالة الأصلي."""
    sig, sig_ents = _signature()
    if not sig:
        return text, entities
    prefix = f"{text}\n\n"
    shift = _u16(prefix)
    merged = list(entities or [])
    for e in sig_ents:
        try:
            merged.append(MessageEntity.de_json({**e, "offset": e["offset"] + shift}, bot))
        except Exception:
            continue
    return prefix + sig, merged or None


def signature_markup(markup):
    """يضيف زر رابط (اختياري) تحت الرسالة يفتح البوت الرئيسي لإنشاء بوت."""
    label = db.get_setting("ct_sig_btn")
    if not (on("sig") and label):
        return markup
    try:
        url = f"https://t.me/{MAIN_BOT.username}"
    except Exception:
        return markup
    rows = [list(r) for r in (markup.inline_keyboard if markup else [])]
    rows.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# الإحالة
# ---------------------------------------------------------------------------
async def record_referral(context, user_id, args):
    """يُستدعى عند أول دخول عضو جديد للبوت الرئيسي."""
    if not on("ref") or not args:
        return
    arg = args[0]
    if not (arg.startswith("ref_") and arg[4:].isdigit()):
        return
    referrer = int(arg[4:])
    if db.get_user(referrer) is None or not db.add_referral(user_id, referrer):
        return
    db.log_activity("referral", referrer, 0, f"new user {user_id}")
    try:
        await context.bot.send_message(referrer, "🎉 انضم عضو جديد عبر رابط دعوتك!")
    except Exception:
        pass


async def ref_command(update, context):
    user = update.effective_user
    if not on("ref"):
        await update.message.reply_text("هذه الميزة غير متاحة حالياً.")
        return
    link = f"https://t.me/{context.bot.username}?start=ref_{user.id}"
    await update.message.reply_text(
        f"🔗 رابط دعوتك:\n{link}\n\n👥 عدد من دعوتهم: {db.count_referrals(user.id)}"
    )


# ---------------------------------------------------------------------------
# النسخ الاحتياطي
# ---------------------------------------------------------------------------
def _make_backup(dest_gz):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = os.path.join(tmpdir, "snapshot.db")
        src, dst = sqlite3.connect(config.DB_NAME), sqlite3.connect(tmp_db)
        try:
            src.backup(dst)      # نسخة متسقة حتى والبوت يعمل
        finally:
            dst.close()
            src.close()
        with open(tmp_db, "rb") as f_in, gzip.open(dest_gz, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)


async def send_backup(bot):
    """يرسل نسخة مضغوطة للمالك. يرجع (نجح، رسالة)."""
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M")
    base = os.path.splitext(os.path.basename(config.DB_NAME))[0]
    path = os.path.join(tempfile.gettempdir(), f"{base}_{stamp}.db.gz")
    try:
        await asyncio.to_thread(_make_backup, path)
        size = os.path.getsize(path)
        if size > BACKUP_MAX_BYTES:
            return False, f"حجم النسخة {size // (1024 * 1024)}MB أكبر من حد تيليجرام (50MB)."
        with open(path, "rb") as f:
            await bot.send_document(
                config.OWNER_ID, f, filename=os.path.basename(path),
                caption=(
                    "💾 نسخة احتياطية لقاعدة البيانات\n"
                    "⚠️ تحتوي بيانات الأعضاء وتوكنات البوتات؛ لا ترسلها لأحد.\n"
                    + ("🔐 التوكنات مشفرة: احتفظ بقيمة TOKEN_ENC_KEY في مكان آمن، فالنسخة لا تُفتح بدونها."
                       if config.TOKEN_ENC_KEY else "")
                ),
            )
        db.set_setting("ct_backup_last", str(int(time.time())))
        return True, "تم إرسال النسخة."
    except Exception as exc:
        logger.warning("backup failed: %s", type(exc).__name__)
        return False, "تعذّر إرسال النسخة."
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def backup_job(context):
    """يعمل كل ساعة: ينظّف السجل القديم ويرسل نسخة إذا حان وقتها."""
    try:
        db.prune_activity(60)
    except Exception:
        pass
    if not on("backup"):
        return
    try:
        last = int(db.get_setting("ct_backup_last") or 0)
    except ValueError:
        last = 0
    if time.time() - last >= _backup_hours() * 3600:
        await send_backup(context.bot)


# ---------------------------------------------------------------------------
# الحظر العام
# ---------------------------------------------------------------------------
def _parse_targets(text):
    ids, unknown = [], []
    for token in (text or "").replace(",", " ").split():
        if token.lstrip("-").isdigit():
            ids.append(int(token))
        else:
            uid = db.find_user_id_by_username(token)
            (ids if uid else unknown).append(uid or token)
    return ids, unknown


def _apply_ban(text, ban):
    ids, unknown = _parse_targets(text)
    done = []
    for uid in ids:
        if uid == config.OWNER_ID:
            continue
        if ban:
            db.add_global_ban(uid)
            done.append(uid)
        elif db.remove_global_ban(uid):
            done.append(uid)
    if done:
        db.log_activity("gban" if ban else "ungban", config.OWNER_ID, 0, " ".join(map(str, done)))
    lines = []
    if done:
        lines.append(("🚫 تم الحظر العام: " if ban else "✅ رُفع الحظر العام: ") + " ".join(map(str, done)))
    if unknown:
        lines.append("❓ لم أجد: " + " ".join(unknown))
    if not lines:
        lines.append("لا شيء تغيّر.")
    return "\n".join(lines)


async def gban_command(update, context):
    if not context.args:
        await update.message.reply_text("الاستخدام: /gban 123456789 أو /gban @username (يقبل عدة أيديات)")
        return
    await update.message.reply_text(_apply_ban(" ".join(context.args), True))


async def ungban_command(update, context):
    if not context.args:
        await update.message.reply_text("الاستخدام: /ungban 123456789 أو /ungban @username")
        return
    await update.message.reply_text(_apply_ban(" ".join(context.args), False))


# ---------------------------------------------------------------------------
# واجهة المالك
# ---------------------------------------------------------------------------
def _btn(text, data, style=None):
    if style:
        return InlineKeyboardButton(text, callback_data=data, style=style)
    return InlineKeyboardButton(text, callback_data=data)


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def _mark(key):
    return "✅ مفعّل" if on(key) else "⚪ مطفأ"


def menu():
    last = int(db.get_setting("ct_backup_last") or 0)
    last_txt = datetime.fromtimestamp(last, TZ).strftime("%m-%d %H:%M") if last else "لم تُرسل بعد"
    text = "\n".join([
        "🎛 مركز التحكم",
        "",
        f"🛠 وضع الصيانة: {_mark('maint')}",
        f"🚫 الحظر العام: {db.count_global_bans()} محظور",
        f"🛡 مكافحة السبام: {_mark('spam')} ({SPAM_LIMIT} رسائل/{SPAM_WINDOW}ث ثم تجاهل {SPAM_MUTE}ث)",
        f"💾 نسخة احتياطية: {_mark('backup')} كل {_backup_hours()} ساعة — آخر نسخة: {last_txt}",
        f"✍️ توقيع البوتات المصنوعة: {_mark('sig')}",
        f"🔗 الإحالة: {_mark('ref')} ({db.count_referrals()} إحالة)",
        "",
        "المطفأ جاهز؛ شغّله بزر عندما تحتاجه.",
    ])
    rows = [
        [_btn("🛠 الصيانة: تشغيل/إيقاف", "cl_ct_t:maint"), _btn("✏️ نص الصيانة", "cl_ct_in:maint")],
        [_btn("🚫 حظر عام", "cl_ct_in:gban", "danger"), _btn("♻️ رفع حظر عام", "cl_ct_in:ungban")],
        [_btn("🛡 السبام: تشغيل/إيقاف", "cl_ct_t:spam")],
        [_btn("💾 النسخ التلقائي", "cl_ct_t:backup"), _btn("⏱ تغيير المدة", "cl_ct_hrs"),
         _btn("📤 أرسل الآن", "cl_ct_bk", "success")],
        [_btn("✍️ التوقيع: تشغيل/إيقاف", "cl_ct_t:sig"), _btn("✏️ نص التوقيع", "cl_ct_in:sig")],
        [_btn("🔘 زر تحت الرسالة", "cl_ct_in:sigbtn")],
        [_btn("🔗 الإحالة: تشغيل/إيقاف", "cl_ct_t:ref")],
        [_btn("📜 سجل النشاط", "cl_ct_log")],
        [_btn("🔙 رجوع", "back_owner")],
    ]
    return text, InlineKeyboardMarkup(rows)


_PROMPTS = {
    "maint": "أرسل نص رسالة الصيانة التي تصل للأعضاء، أو «افتراضي».",
    "sig": ("أرسل نص التوقيع الذي يُضاف لرسائل البوتات المصنوعة، أو «افتراضي».\n"
            "يمكنك استعمال التنسيق والروابط (اكتب النص ثم حدّده وأضف رابطاً من تنسيق تيليجرام) "
            "وسيُحفظ كما هو."),
    "sigbtn": ("أرسل نص زر الرابط الذي يظهر تحت رسائل البوتات المصنوعة ويفتح بوتك الرئيسي "
               "(مثال: 🤖 أنشئ بوتك الآن)، أو «حذف» لإزالته."),
    "gban": "أرسل أيدي (أو @يوزر) العضو المراد حظره عاماً. يمكن عدة أيديات بمسافة بينها.",
    "ungban": "أرسل أيدي (أو @يوزر) العضو المراد رفع الحظر العام عنه.",
}


def _log_text():
    rows = db.recent_activity(25)
    if not rows:
        return "📜 لا يوجد نشاط مسجل بعد."
    lines = ["📜 آخر النشاطات:\n"]
    for ts, actor, bot_id, action, detail in rows:
        when = datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M")
        where = f"بوت {bot_id}" if bot_id else "الرئيسي"
        lines.append(f"{when} · {where} · {action}" + (f" — {detail}" if detail else ""))
    return "\n".join(lines)[:3900]


async def main_callback(update, context):
    query = update.callback_query
    if update.effective_user.id != config.OWNER_ID:
        await query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
        return
    action, _, arg = (query.data or "").partition(":")
    context.user_data.pop("cl_expect", None)

    if action == "cl_ct_t" and arg in DEFAULTS:
        _toggle(arg)
        db.log_activity(f"toggle_{arg}", config.OWNER_ID, 0, "on" if on(arg) else "off")
    elif action == "cl_ct_hrs":
        steps = BACKUP_STEPS
        cur = _backup_hours()
        nxt = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else steps[0]
        db.set_setting("ct_backup_hours", str(nxt))
    elif action == "cl_ct_in" and arg in _PROMPTS:
        await query.answer()
        context.user_data["cl_expect"] = f"ct_{arg}"
        await _edit(
            query, _PROMPTS[arg] + "\nللإلغاء أرسل «إلغاء».",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_ct")]]),
        )
        return
    elif action == "cl_ct_bk":
        await query.answer("⏳ جاري تجهيز النسخة...")
        ok, msg = await send_backup(context.bot)
        if not ok:
            await context.bot.send_message(config.OWNER_ID, f"⚠️ {msg}")
    elif action == "cl_ct_log":
        await query.answer()
        await _edit(query, _log_text(), InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_ct")]]))
        return
    await query.answer()
    text, markup = menu()
    await _edit(query, text, markup)


async def main_input(update, context, expect):
    """إدخال المالك النصي من مركز التحكم. يرجع True إذا اكتمل."""
    msg = update.message
    if not msg.text:
        await msg.reply_text("أرسل نصاً، أو «إلغاء».")
        return False
    kind = expect.partition("_")[2]
    text = msg.text.strip()
    back = InlineKeyboardMarkup([[_btn("🎛 مركز التحكم", "cl_ct")]])
    if kind in ("maint", "sig"):
        key = f"ct_{kind}_text"
        if text in ("افتراضي", "الافتراضي"):
            db.set_setting(key, "")
            db.set_setting(f"ct_{kind}_entities", "")
            await msg.reply_text("✅ رجع النص للافتراضي.", reply_markup=back)
        else:
            db.set_setting(key, msg.text)
            db.set_setting(f"ct_{kind}_entities", entities_json(msg))   # يحفظ الروابط والتنسيق
            await msg.reply_text("✅ تم الحفظ مع التنسيق والروابط.", reply_markup=back)
    elif kind == "sigbtn":
        if text in ("حذف", "إزالة", "مسح"):
            db.set_setting("ct_sig_btn", "")
            await msg.reply_text("✅ حُذف الزر.", reply_markup=back)
        elif len(text) > 40:
            await msg.reply_text("النص طويل (الحد 40 حرفاً). أرسل نصاً أقصر.")
            return False
        else:
            db.set_setting("ct_sig_btn", text)
            await msg.reply_text("✅ تم الحفظ. يظهر الزر عندما يكون التوقيع مفعّلاً.", reply_markup=back)
    elif kind in ("gban", "ungban"):
        await msg.reply_text(_apply_ban(text, kind == "gban"), reply_markup=back)
    return True


def register_main_handlers(application):
    global MAIN_BOT
    MAIN_BOT = application.bot
    private_owner = filters.ChatType.PRIVATE & filters.User(config.OWNER_ID)
    application.add_handler(CommandHandler("gban", gban_command, filters=private_owner))
    application.add_handler(CommandHandler("ungban", ungban_command, filters=private_owner))
    application.add_handler(CommandHandler("ref", ref_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(main_callback, pattern="^cl_ct"))
    if application.job_queue is not None:
        application.job_queue.run_repeating(backup_job, interval=3600, first=120, name="owner_backup")
