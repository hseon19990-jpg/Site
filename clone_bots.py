"""قسم «قم بإنشاء بوت».

العضو يضغط الزر في البوت الرئيسي ويرسل توكن بوته، فيشغّل السايت له بوتاً
مماثلاً داخل نفس العملية (process) بخصائص خاصة به:
  - حظر / إلغاء الحظر (بالرد أو بالأيدي أو باليوزر)
  - إذاعة لأعضاء بوته
  - تفعيل/إلغاء زر ولد وأميرة وتغيير نصوصهما
  - تغيير رسالة الترحيب ورسالة الإرسال
  - كليشات مجهزة من المالك تصل للجميع، وأي تعديل من صاحب البوت يبقى له.

المالك الرئيسي (OWNER_ID) يملك إضافةً:
  - تعيين كليشة الترحيب/الإرسال الافتراضية للبوتات
  - إذاعة شاملة (لأعضاء السايت + أصحاب البوتات + أعضاء البوتات المصنوعة)
  - قائمة البوتات وحذف أي بوت
"""
import asyncio
import json
import logging
import re
from html import escape as html_escape

from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
    ReplyParameters, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

import config
import database as db

logger = logging.getLogger(__name__)

# البوتات المصنوعة العاملة الآن: bot_id -> Application
RUNNING: dict = {}

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
CLONE_UPDATES = ["message", "callback_query"]

CANCEL_WORDS = {"إلغاء", "الغاء", "/cancel", "cancel"}
RESET_WORDS = {"افتراضي", "الافتراضي", "استرجاع"}
CLEAR_WORDS = {"حذف", "إزالة", "مسح"}
PANEL_RE = re.compile(r"(?i)^\s*(?:/admin(?:@\w+)?|/panel|ادمن|أدمن|لوحة|لوحة التحكم)\s*$")
BAN_RE = re.compile(r"^\s*(?:/ban(?:@\w+)?|حظر)(?:\s+(\S+))?\s*$", re.I)
UNBAN_RE = re.compile(
    r"^\s*(?:/unban(?:@\w+)?|(?:الغاء|إلغاء|الغا|رفع)\s*(?:ال)?حظر)(?:\s+(\S+))?\s*$",
    re.I,
)

# نصوص افتراضية إذا لم يضع المالك كليشة ولم يغيّر صاحب البوت شيئاً.
DEFAULTS = {
    "welcome": "أهلاً بك 👋 أرسل رسالتك وسأوصلها لصاحب البوت.",
    "ack": "تم إرسال رسالتك ✅",
}
GENDER_DEFAULTS = {
    "male_button": "ولد",
    "female_button": "أميرة",
    "male_label": "رسالة الولد",
    "female_label": "رسالة الأميرة",
}
TEMPLATE_NAMES = {"welcome": "الترحيب", "ack": "الإرسال"}


# ---------------------------------------------------------------------------
# أدوات عامة
# ---------------------------------------------------------------------------
def _btn(text, data, style=None):
    if style:
        return InlineKeyboardButton(text, callback_data=data, style=style)
    return InlineKeyboardButton(text, callback_data=data)


def _is_master(user) -> bool:
    return user is not None and user.id == config.OWNER_ID


def _entities_json(message) -> str:
    return json.dumps(
        [e.to_dict() for e in (message.entities or [])], ensure_ascii=False
    )


def _parse_entities(raw, bot):
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        return [MessageEntity.de_json(e, bot) for e in data] or None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _retry_seconds(exc: RetryAfter) -> float:
    value = exc.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _copy_with_retry(bot, to_chat, from_chat, message_id) -> bool:
    for _ in range(2):
        try:
            await bot.copy_message(
                chat_id=to_chat, from_chat_id=from_chat, message_id=message_id
            )
            return True
        except RetryAfter as exc:
            await asyncio.sleep(_retry_seconds(exc) + 1)
        except Exception:
            return False
    return False


async def _send_text_entities(bot, chat_id, text, entities, reply_markup=None):
    """إرسال نص بتنسيقه (وإيموجيه المميز إن أمكن)، وإلا يُرسل بدون تنسيق."""
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text, entities=entities, reply_markup=reply_markup
        )
    except BadRequest:
        if not entities:
            raise
        # الإيموجي المميز لا يعمل في كل البوتات؛ نرجع للنص العادي.
        return await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup
        )


# ---------------------------------------------------------------------------
# الكليشات والإعدادات: إعداد صاحب البوت يتفوق دائماً على كليشة المالك
# ---------------------------------------------------------------------------
def get_clone_text(bot_id, name):
    """(النص، كيانات التنسيق) — الأولوية: ما ضبطه صاحب البوت، ثم كليشة المالك، ثم الافتراضي."""
    own = db.clone_get_setting(bot_id, f"{name}_message")
    if own is not None:
        return own, db.clone_get_setting(bot_id, f"{name}_entities")
    template = db.get_setting(f"tpl_{name}_message")
    if template:
        return template, db.get_setting(f"tpl_{name}_entities")
    return DEFAULTS[name], None


def _gender_cfg(bot_id):
    cfg = {
        key: db.clone_get_setting(bot_id, key) or default
        for key, default in GENDER_DEFAULTS.items()
    }
    cfg["enabled"] = (db.clone_get_setting(bot_id, "gender_enabled") or "1") == "1"
    return cfg


async def _send_cfg(bot, chat_id, bot_id, name, reply_markup=None):
    text, raw = get_clone_text(bot_id, name)
    await _send_text_entities(bot, chat_id, text, _parse_entities(raw, bot), reply_markup)


# ---------------------------------------------------------------------------
# تشغيل وإيقاف البوتات المصنوعة
# ---------------------------------------------------------------------------
def _poll_error_callback(bot_id):
    def callback(exc):
        # لا نسجل نص الاستثناء حتى لا يظهر التوكن في السجلات.
        logger.warning("clone %s polling error: %s", bot_id, type(exc).__name__)
    return callback


async def _clone_error(update, context):
    logger.warning("clone handler error: %s", type(context.error).__name__)


async def _safe_shutdown(app):
    for step in ("updater_stop", "stop", "shutdown"):
        try:
            if step == "updater_stop":
                if app.updater and app.updater.running:
                    await app.updater.stop()
            elif step == "stop":
                if app.running:
                    await app.stop()
            else:
                await app.shutdown()
        except Exception:
            pass


async def start_clone(bot_id, token, owner_id):
    """يشغّل بوتاً مصنوعاً. يرفع InvalidToken إن كان التوكن مرفوضاً."""
    if bot_id in RUNNING:
        await stop_clone(bot_id)
    app = _build_clone_app(bot_id, token, owner_id)
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            allowed_updates=CLONE_UPDATES,
            error_callback=_poll_error_callback(bot_id),
        )
    except Exception:
        await _safe_shutdown(app)
        raise
    RUNNING[bot_id] = app


async def stop_clone(bot_id):
    app = RUNNING.pop(bot_id, None)
    if app is not None:
        await _safe_shutdown(app)


async def restore_clones(application):
    """عند تشغيل البوت الرئيسي: أعد تشغيل كل البوتات المحفوظة."""
    clones = db.list_clones(active_only=True)
    sem = asyncio.Semaphore(8)

    async def one(clone):
        async with sem:
            try:
                await start_clone(clone["bot_id"], clone["token"], clone["owner_id"])
            except InvalidToken:
                db.set_clone_active(clone["bot_id"], False)
                try:
                    await application.bot.send_message(
                        chat_id=clone["owner_id"],
                        text=(
                            f"⚠️ توقف بوتك @{clone['username']} لأن التوكن لم يعد صالحاً.\n"
                            "افتح «🤖 قم بإنشاء بوت» ثم «تحديث التوكن» وأرسل التوكن الجديد."
                        ),
                    )
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(
                    "could not start clone %s: %s", clone["bot_id"], type(exc).__name__
                )

    if clones:
        await asyncio.gather(*(one(c) for c in clones))
    logger.info("clone bots running: %d/%d", len(RUNNING), len(clones))


async def stop_all_clones(application):
    for bot_id in list(RUNNING):
        await stop_clone(bot_id)


# ---------------------------------------------------------------------------
# الجانب الرئيسي: العضو يصنع بوته
# ---------------------------------------------------------------------------
def _create_instructions(refresh_for=None):
    head = (
        f"🔄 أرسل التوكن الجديد للبوت @{refresh_for}."
        if refresh_for
        else "🤖 إنشاء بوت خاص بك"
    )
    return (
        f"{head}\n\n"
        "1) افتح @BotFather وأرسل /newbot واتبع الخطوات.\n"
        "2) انسخ التوكن الذي يعطيك إياه وأرسله هنا في رسالة واحدة.\n\n"
        "🔒 سأحذف رسالة التوكن بعد قراءتها، ولا تشاركه مع أي أحد.\n"
        "للإلغاء أرسل «إلغاء»."
    )


async def _show_create_entry(context, user):
    db.add_user(user.id, user.full_name, user.username)
    row = db.get_user(user.id)
    if row and row[4] == 1:
        await context.bot.send_message(user.id, "أنت محظور من استخدام البوت.")
        return

    existing = db.get_clone_by_owner(user.id)
    if existing:
        context.user_data.pop("cl_expect", None)
        members, _, _ = db.clone_stats(existing["bot_id"])
        running = existing["bot_id"] in RUNNING
        status = "يعمل ✅" if running else "متوقف ⛔"
        text = (
            f"🤖 بوتك: @{existing['username']}\n"
            f"الحالة: {status}\n"
            f"👥 الأعضاء: {members}\n\n"
            "افتح بوتك وأرسل «ادمن» لفتح لوحة التحكم."
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 فتح البوت", url=f"https://t.me/{existing['username']}")],
            [_btn("🔄 تحديث التوكن", "cl_token", "success")],
            [_btn("🗑 حذف بوتي", "cl_delete", "danger")],
        ])
        await context.bot.send_message(user.id, text, reply_markup=markup)
        return

    context.user_data["cl_expect"] = "token"
    await context.bot.send_message(
        user.id,
        _create_instructions(),
        reply_markup=InlineKeyboardMarkup([[_btn("❌ إلغاء", "cl_cancel")]]),
    )


async def cl_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_create_entry(context, update.effective_user)


async def cl_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _show_create_entry(context, update.effective_user)


async def cl_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    existing = db.get_clone_by_owner(update.effective_user.id)
    if existing is None:
        await _show_create_entry(context, update.effective_user)
        return
    context.user_data["cl_expect"] = "token"
    await context.bot.send_message(
        update.effective_user.id,
        _create_instructions(refresh_for=existing["username"]),
        reply_markup=InlineKeyboardMarkup([[_btn("❌ إلغاء", "cl_cancel")]]),
    )


async def cl_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("cl_expect", None)
    context.user_data.pop("cl_bc_msg", None)
    await _edit(query, "تم الإلغاء.")


async def cl_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    existing = db.get_clone_by_owner(update.effective_user.id)
    if existing is None:
        await _edit(query, "لا يوجد بوت مسجل باسمك.")
        return
    await _edit(
        query,
        f"⚠️ سيتم إيقاف @{existing['username']} وحذف أعضائه وإعداداته نهائياً. متأكد؟",
        InlineKeyboardMarkup([[
            _btn("✅ نعم، احذفه", "cl_delete_yes", "danger"),
            _btn("🔙 رجوع", "cl_cancel"),
        ]]),
    )


async def cl_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    existing = db.get_clone_by_owner(update.effective_user.id)
    if existing is None:
        await _edit(query, "لا يوجد بوت مسجل باسمك.")
        return
    await stop_clone(existing["bot_id"])
    db.delete_clone(existing["bot_id"])
    await _edit(query, f"🗑 تم حذف البوت @{existing['username']}.")


async def _handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    token = (msg.text or "").strip()

    # نحذف رسالة التوكن من المحادثة فوراً.
    try:
        await msg.delete()
    except Exception:
        pass

    async def say(text, markup=None):
        await context.bot.send_message(user.id, text, reply_markup=markup)

    if not TOKEN_RE.match(token):
        await say(
            "❌ هذا لا يبدو توكن بوت صحيحاً. انسخه من @BotFather كما هو وأرسله مجدداً، "
            "أو أرسل «إلغاء»."
        )
        return

    try:
        async with Bot(token) as probe:
            me = await probe.get_me()
    except InvalidToken:
        await say("❌ هذا التوكن مرفوض من تيليجرام. تأكد منه وأرسله مجدداً، أو أرسل «إلغاء».")
        return
    except Exception:
        await say("⚠️ تعذّر التحقق من التوكن الآن. حاول مرة أخرى بعد قليل.")
        return

    if me.id == context.bot.id:
        await say("❌ هذا توكن البوت الرئيسي، أرسل توكن بوت جديد من @BotFather.")
        return

    other = db.get_clone(me.id)
    if other and other["owner_id"] != user.id:
        await say("❌ هذا البوت مسجل لدى شخص آخر.")
        return
    existing = db.get_clone_by_owner(user.id)
    if existing and existing["bot_id"] != me.id:
        await say(
            f"❌ لديك بوت مسجل (@{existing['username']}). احذفه أولاً من «🤖 قم بإنشاء بوت» "
            "إذا أردت استبداله ببوت آخر، أو أرسل توكن نفس البوت لتحديثه."
        )
        return

    is_new = existing is None
    db.save_clone(me.id, user.id, token, me.username, me.full_name)
    try:
        await start_clone(me.id, token, user.id)
    except Exception as exc:
        if is_new:
            db.delete_clone(me.id)
        else:
            db.set_clone_active(me.id, False)
        logger.warning("clone start failed: %s", type(exc).__name__)
        await say("⚠️ تعذّر تشغيل البوت. تأكد أن التوكن صحيح وأن البوت غير مستخدم في مكان آخر، ثم حاول مجدداً.")
        return

    context.user_data.pop("cl_expect", None)
    await say(
        f"✅ تم {'إنشاء' if is_new else 'تحديث'} بوتك @{me.username} وهو يعمل الآن.\n\n"
        "1) افتح بوتك واضغط Start (مرة واحدة حتى تصلك الرسائل).\n"
        "2) أرسل «ادمن» داخل بوتك لفتح لوحة التحكم: الترحيب، رسالة الإرسال، "
        "أزرار ولد/أميرة، الإذاعة، والحظر.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔗 فتح بوتي", url=f"https://t.me/{me.username}")]]),
    )
    if is_new and user.id != config.OWNER_ID:
        try:
            await context.bot.send_message(
                config.OWNER_ID,
                "🤖 تم إنشاء بوت جديد\n"
                f"🔗 @{me.username}\n"
                f"👤 المالك: {user.full_name}"
                + (f" (@{user.username})" if user.username else "")
                + f" — {user.id}",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# إذاعة المالك الشاملة
# ---------------------------------------------------------------------------
_MEDIA_KINDS = ("photo", "video", "animation", "document", "audio", "voice")
_MEDIA_SENDERS = {
    "photo": "send_photo", "video": "send_video", "animation": "send_animation",
    "document": "send_document", "audio": "send_audio", "voice": "send_voice",
}


def _extract_payload(m):
    """وصف رسالة المالك بحيث يمكن إعادة إرسالها عبر أي بوت (نص/صورة/فيديو/ملف/صوت)."""
    entities = [e.to_dict() for e in (m.entities or m.caption_entities or [])]
    if m.text is not None:
        return {"kind": "text", "text": m.text, "entities": entities}
    caption = m.caption or None
    if m.photo:
        return {"kind": "photo", "file_id": m.photo[-1].file_id,
                "text": caption, "entities": entities}
    for kind in ("video", "animation", "document", "audio", "voice"):
        media = getattr(m, kind, None)
        if media is not None:
            return {"kind": kind, "file_id": media.file_id, "text": caption,
                    "entities": entities, "filename": getattr(media, "file_name", None)}
    return None


async def _send_payload(bot, chat_id, payload, media) -> bool:
    for _ in range(2):
        for use_entities in (True, False):
            entities = (
                [MessageEntity.de_json(e, bot) for e in payload["entities"]] or None
                if use_entities else None
            )
            try:
                if payload["kind"] == "text":
                    await bot.send_message(chat_id=chat_id, text=payload["text"], entities=entities)
                else:
                    kwargs = {
                        "chat_id": chat_id,
                        payload["kind"]: media,
                        "caption": payload.get("text"),
                        "caption_entities": entities,
                    }
                    if payload["kind"] == "document" and payload.get("filename"):
                        kwargs["filename"] = payload["filename"]
                    await getattr(bot, _MEDIA_SENDERS[payload["kind"]])(**kwargs)
                return True
            except RetryAfter as exc:
                await asyncio.sleep(_retry_seconds(exc) + 1)
                break
            except BadRequest:
                if use_entities and payload["entities"]:
                    continue
                return False
            except Exception:
                return False
    return False


async def _cascade_broadcast(main_bot, pending, scope):
    payload = pending["payload"]
    src_chat, src_msg = pending["chat_id"], pending["message_id"]
    clones = db.list_clones(active_only=True)

    # (1) أعضاء السايت + أصحاب البوتات عبر البوت الرئيسي.
    main_ids = set(db.get_all_users()) | {c["owner_id"] for c in clones}
    main_ids.discard(config.OWNER_ID)
    main_ok = 0
    for uid in main_ids:
        if await _copy_with_retry(main_bot, uid, src_chat, src_msg):
            main_ok += 1
        await asyncio.sleep(0.05)

    # (2) أعضاء كل بوت مصنوع عبر بوتهم نفسه.
    members_ok = members_total = skipped = 0
    media_note = ""
    if scope == "all" and payload:
        media = None
        if payload["kind"] != "text":
            try:
                tg_file = await main_bot.get_file(payload["file_id"])
                media = bytes(await tg_file.download_as_bytearray())
            except Exception:
                media_note = "\n⚠️ تعذّر تنزيل المرفق (قد يكون كبيراً)، لم يُرسل لأعضاء البوتات."
                clones = []

        async def run(clone):
            app = RUNNING.get(clone["bot_id"])
            ids = db.clone_user_ids(clone["bot_id"], exclude_user_id=clone["owner_id"])
            if app is None:
                return 0, len(ids), 1
            ok = 0
            for uid in ids:
                if await _send_payload(app.bot, uid, payload, media):
                    ok += 1
                await asyncio.sleep(0.05)
            return ok, len(ids), 0

        for ok, total, skip in await asyncio.gather(*(run(c) for c in clones)):
            members_ok += ok
            members_total += total
            skipped += skip

    lines = [
        "✅ انتهت الإذاعة",
        f"• أعضاء السايت وأصحاب البوتات: {main_ok}/{len(main_ids)}",
    ]
    if scope == "all":
        lines.append(f"• أعضاء البوتات المصنوعة: {members_ok}/{members_total}")
        if skipped:
            lines.append(f"• بوتات متوقفة تم تجاوزها: {skipped}")
    text = "\n".join(lines) + media_note
    try:
        await main_bot.send_message(config.OWNER_ID, text)
    except Exception:
        logger.exception("could not send cascade broadcast report")


# ---------------------------------------------------------------------------
# لوحة المالك: البوتات المصنوعة
# ---------------------------------------------------------------------------
async def _master_only(update) -> bool:
    if _is_master(update.effective_user):
        return True
    await update.callback_query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
    return False


async def cl_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    await query.answer()
    clones = db.list_clones()
    running = sum(1 for c in clones if c["bot_id"] in RUNNING)
    members = sum(db.clone_stats(c["bot_id"])[0] for c in clones)

    def mark(name):
        return "✅" if db.get_setting(f"tpl_{name}_message") else "⚪"

    text = (
        "🤖 البوتات المصنوعة\n\n"
        f"عدد البوتات: {len(clones)} (يعمل منها {running})\n"
        f"إجمالي أعضائها: {members}\n\n"
        f"كليشة الترحيب: {mark('welcome')}   كليشة الإرسال: {mark('ack')}\n"
        "الكليشة تصل لكل البوتات التي لم يغيّر صاحبها الرسالة بنفسه."
    )
    markup = InlineKeyboardMarkup([
        [_btn("📋 قائمة البوتات", "cl_list")],
        [_btn("📝 كليشة الترحيب", "cl_tpl:welcome"), _btn("📝 كليشة الإرسال", "cl_tpl:ack")],
        [_btn("📢 إذاعة شاملة", "cl_bc", "success")],
        [_btn("🔙 رجوع", "back_owner")],
    ])
    await _edit(query, text, markup)


async def cl_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    await query.answer()
    clones = db.list_clones()
    if not clones:
        await _edit(query, "لا توجد بوتات مصنوعة بعد.",
                    InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_admin")]]))
        return
    lines, rows = ["📋 البوتات المصنوعة:\n"], []
    for i, c in enumerate(clones[:30], 1):
        members = db.clone_stats(c["bot_id"])[0]
        state = "✅" if c["bot_id"] in RUNNING else "⛔"
        lines.append(f"{i}) {state} @{c['username']} — المالك {c['owner_id']} — {members} عضو")
    for c in clones[:20]:
        rows.append([_btn(f"🗑 @{c['username']}", f"cl_kill:{c['bot_id']}")])
    if len(clones) > 30:
        lines.append(f"\n… و{len(clones) - 30} بوت آخر.")
    rows.append([_btn("🔙 رجوع", "cl_admin")])
    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def cl_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    await query.answer()
    clone = db.get_clone(int((query.data or "").partition(":")[2] or 0))
    if clone is None:
        await _edit(query, "البوت غير موجود.", InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_list")]]))
        return
    await _edit(
        query,
        f"⚠️ حذف @{clone['username']} نهائياً مع أعضائه وإعداداته؟",
        InlineKeyboardMarkup([[
            _btn("✅ نعم، احذف", f"cl_kill_yes:{clone['bot_id']}", "danger"),
            _btn("🔙 رجوع", "cl_list"),
        ]]),
    )


async def cl_kill_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    await query.answer()
    clone = db.get_clone(int((query.data or "").partition(":")[2] or 0))
    if clone is None:
        await _edit(query, "البوت غير موجود.", InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_list")]]))
        return
    await stop_clone(clone["bot_id"])
    db.delete_clone(clone["bot_id"])
    await _edit(query, f"🗑 تم حذف @{clone['username']}.",
                InlineKeyboardMarkup([[_btn("🔙 للقائمة", "cl_list")]]))
    try:
        await context.bot.send_message(
            clone["owner_id"], f"ℹ️ تم حذف بوتك @{clone['username']} من قبل الإدارة."
        )
    except Exception:
        pass


async def cl_tpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    name = (query.data or "").partition(":")[2]
    if name not in TEMPLATE_NAMES:
        await query.answer("غير معروف.", show_alert=True)
        return
    await query.answer()
    context.user_data["cl_expect"] = f"tpl_{name}"
    await _edit(
        query,
        f"أرسل كليشة {TEMPLATE_NAMES[name]} للبوتات (تدعم التنسيق والإيموجي المميز).\n\n"
        "• تصل للبوتات الجديدة والحالية التي لم يغيّر صاحبها هذه الرسالة.\n"
        "• من غيّرها بنفسه تبقى عنده ولو غيّرت الكليشة لاحقاً.\n"
        "• أرسل «حذف» لإزالة الكليشة (يعود النص الافتراضي)، أو «إلغاء».",
        InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_admin")]]),
    )


async def cl_bc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    await query.answer()
    context.user_data["cl_expect"] = "cascade_msg"
    await _edit(
        query,
        "📢 أرسل الرسالة التي تريد إذاعتها (نص أو صورة أو فيديو أو ملف أو صوت).\n"
        "بعدها تختار الجمهور وتؤكد الإرسال. للإلغاء أرسل «إلغاء».",
        InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_admin")]]),
    )


async def cl_bc_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _master_only(update):
        return
    scope = (query.data or "").partition(":")[2]
    pending = context.user_data.pop("cl_bc_msg", None)
    if pending is None or scope not in {"all", "mine"}:
        await query.answer("انتهت الجلسة، ابدأ الإذاعة من جديد.", show_alert=True)
        return
    await query.answer()
    await _edit(query, "⏳ بدأت الإذاعة، سيصلك تقرير عند الانتهاء.")
    context.application.create_task(_cascade_broadcast(context.bot, pending, scope))


# ---------------------------------------------------------------------------
# معالج الإدخال المعلّق في البوت الرئيسي (توكن / كليشات / إذاعة المالك)
# ---------------------------------------------------------------------------
async def main_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    expect = context.user_data.get("cl_expect")
    if not expect or msg is None:
        return
    user = update.effective_user
    text = (msg.text or "").strip()

    if text in CANCEL_WORDS:
        context.user_data.pop("cl_expect", None)
        await context.bot.send_message(user.id, "تم الإلغاء.")
        raise ApplicationHandlerStop
    if text and PANEL_RE.match(text):
        context.user_data.pop("cl_expect", None)
        return  # يكمل لمعالج لوحة المالك

    if expect == "token":
        if not msg.text:
            await context.bot.send_message(user.id, "أرسل التوكن كنص، أو أرسل «إلغاء».")
        else:
            await _handle_token(update, context)
        raise ApplicationHandlerStop

    if not _is_master(user):
        context.user_data.pop("cl_expect", None)
        return

    if expect in ("tpl_welcome", "tpl_ack"):
        name = expect.partition("_")[2]
        if msg.text is None:
            await msg.reply_text("أرسل نصاً (يدعم التنسيق والإيموجي المميز)، أو «إلغاء».")
            raise ApplicationHandlerStop
        if text in CLEAR_WORDS:
            db.set_setting(f"tpl_{name}_message", "")
            db.set_setting(f"tpl_{name}_entities", "")
            await msg.reply_text(f"✅ تم حذف كليشة {TEMPLATE_NAMES[name]}.")
        else:
            db.set_setting(f"tpl_{name}_message", msg.text)
            db.set_setting(f"tpl_{name}_entities", _entities_json(msg))
            await msg.reply_text(
                f"✅ تم حفظ كليشة {TEMPLATE_NAMES[name]}. ستصل للبوتات التي لم يغيّر "
                "صاحبها هذه الرسالة."
            )
        context.user_data.pop("cl_expect", None)
        raise ApplicationHandlerStop

    if expect == "cascade_msg":
        payload = _extract_payload(msg)
        clones = db.list_clones(active_only=True)
        main_ids = set(db.get_all_users()) | {c["owner_id"] for c in clones}
        main_ids.discard(config.OWNER_ID)
        members = sum(
            len(db.clone_user_ids(c["bot_id"], exclude_user_id=c["owner_id"])) for c in clones
        )
        context.user_data["cl_bc_msg"] = {
            "chat_id": msg.chat_id, "message_id": msg.message_id, "payload": payload,
        }
        context.user_data.pop("cl_expect", None)
        rows = []
        if payload is not None:
            rows.append([_btn(f"📨 للكل ({len(main_ids) + members})", "cl_bc_send:all", "success")])
        rows.append([_btn(f"👥 أعضاء السايت وأصحاب البوتات فقط ({len(main_ids)})", "cl_bc_send:mine")])
        rows.append([_btn("❌ إلغاء", "cl_cancel")])
        note = ""
        if payload is None:
            note = ("\n\n⚠️ هذا النوع من الرسائل يصل فقط لأعضاء السايت وأصحاب البوتات. "
                    "لأعضاء البوتات المصنوعة أرسل نصاً أو صورة أو فيديو أو ملفاً أو صوتاً.")
        await msg.reply_text(
            "اختر الجمهور:\n"
            f"• أعضاء السايت وأصحاب البوتات: {len(main_ids)}\n"
            f"• أعضاء البوتات المصنوعة: {members}" + note,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        raise ApplicationHandlerStop

    context.user_data.pop("cl_expect", None)


async def reset_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي زر آخر يلغي الإدخال المعلّق حتى لا تُلتقط الرسالة التالية بالخطأ."""
    context.user_data.pop("cl_expect", None)


def register_main_handlers(application):
    """يضاف إلى تطبيق البوت الرئيسي."""
    private = filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE
    application.add_handler(
        CallbackQueryHandler(reset_pending, pattern=r"^(?!cl_)"), group=-2
    )
    application.add_handler(
        MessageHandler(
            private & ~filters.COMMAND & ~filters.StatusUpdate.ALL, main_pending_input
        ),
        group=-1,
    )
    application.add_handler(CommandHandler("createbot", cl_create_command, filters=private))
    for pattern, handler in (
        ("^cl_create$", cl_create),
        ("^cl_token$", cl_token),
        ("^cl_cancel$", cl_cancel),
        ("^cl_delete$", cl_delete),
        ("^cl_delete_yes$", cl_delete_yes),
        ("^cl_admin$", cl_admin),
        ("^cl_list$", cl_list),
        ("^cl_kill:", cl_kill),
        ("^cl_kill_yes:", cl_kill_yes),
        ("^cl_tpl:", cl_tpl),
        ("^cl_bc$", cl_bc),
        ("^cl_bc_send:", cl_bc_send),
    ):
        application.add_handler(CallbackQueryHandler(handler, pattern=pattern))


# ===========================================================================
# البوت المصنوع نفسه (يعمل لكل عضو بتوكنه)
# ===========================================================================
def _cid(context):
    return context.application.bot_data["clone_id"]


def _oid(context):
    return context.application.bot_data["clone_owner"]


def _gender_keyboard(bot_id):
    cfg = _gender_cfg(bot_id)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(cfg["male_button"], callback_data="set_gender:male"),
        InlineKeyboardButton(cfg["female_button"], callback_data="set_gender:female"),
    ]])


def _panel_markup():
    return InlineKeyboardMarkup([
        [_btn("✏️ رسالة الترحيب", "o_welcome"), _btn("✏️ رسالة الإرسال", "o_ack")],
        [_btn("🎚 أزرار ولد / أميرة", "o_gender")],
        [_btn("📢 إذاعة", "o_bc", "success"), _btn("📊 إحصائيات", "o_stats")],
        [_btn("🚫 الحظر وإلغاء الحظر", "o_help")],
    ])


async def clone_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    cid, oid = _cid(context), _oid(context)
    db.clone_add_user(cid, user.id, user.full_name, user.username)

    if user.id != oid and db.clone_mark_start_notified(cid, user.id):
        card = (
            "🔔 <b>دخول عضو جديد لأول مرة</b>\n"
            f"👤 الاسم: {html_escape(user.full_name)}\n"
        )
        if user.username:
            card += f"🔗 اسم المستخدم: @{html_escape(user.username)}\n"
        card += f"🆔 الأيدي: <code>{user.id}</code>"
        try:
            await context.bot.send_message(oid, card, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    markup = None
    if user.id == oid:
        markup = InlineKeyboardMarkup([[_btn("🛠 لوحة التحكم", "o_panel", "success")]])
    await _send_cfg(context.bot, user.id, cid, "welcome", markup)

    if user.id != oid and _gender_cfg(cid)["enabled"]:
        row = db.clone_get_user(cid, user.id)
        if row and row["gender"] not in ("male", "female"):
            await context.bot.send_message(
                user.id, "قبل إرسال الرسائل، اختر من الأزرار:", reply_markup=_gender_keyboard(cid)
            )


async def clone_set_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    gender = (query.data or "").partition(":")[2]
    if gender not in {"male", "female"}:
        await query.answer("الاختيار غير صالح.", show_alert=True)
        return
    cid = _cid(context)
    db.clone_add_user(cid, user.id, user.full_name, user.username)
    db.clone_set_gender(cid, user.id, gender)
    await query.answer("تم حفظ اختيارك.")
    await context.bot.send_message(user.id, "تم حفظ اختيارك. يمكنك الآن إرسال رسالتك.")


async def clone_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    cid, oid = _cid(context), _oid(context)
    db.clone_add_user(cid, user.id, user.full_name, user.username)
    row = db.clone_get_user(cid, user.id)

    if row["is_banned"]:
        await msg.reply_text("أنت محظور من استخدام البوت.")
        return

    cfg = _gender_cfg(cid)
    gender = row["gender"] if row["gender"] in ("male", "female") else None
    if cfg["enabled"] and gender is None:
        await msg.reply_text(
            "اختر من الأزرار أولاً، ثم أعد إرسال رسالتك:",
            reply_markup=_gender_keyboard(cid),
        )
        return

    count = db.clone_increment_messages(cid, user.id)
    card = f"🆔 الأيدي: <code>{user.id}</code>\n"
    card += (
        f'📩 رسالة جديدة من: <a href="tg://openmessage?user_id={user.id}">'
        f"{html_escape(user.full_name)}</a>\n"
    )
    if user.username:
        card += f"👤 اسم المستخدم: @{html_escape(user.username)}\n"
    card += f"📊 عدد الرسائل: {count}"
    if cfg["enabled"] and gender:
        shown = cfg["male_button"] if gender == "male" else cfg["female_button"]
        card += f"\n⚧ الجنس: {html_escape(shown)}"

    label = "رسالة جديدة"
    if cfg["enabled"] and gender:
        label = cfg["male_label"] if gender == "male" else cfg["female_label"]

    try:
        info = await context.bot.send_message(oid, card, parse_mode=ParseMode.HTML)
        if msg.text is not None:
            content = await context.bot.send_message(
                oid,
                f"<b>{html_escape(label)}:</b>\n{html_escape(msg.text)}",
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(message_id=info.message_id),
            )
        else:
            content = await context.bot.copy_message(
                chat_id=oid,
                from_chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                reply_parameters=ReplyParameters(message_id=info.message_id),
            )
    except Exception:
        logger.warning("clone %s could not deliver a message to its owner", cid)
        await msg.reply_text("تعذّر إيصال رسالتك الآن، حاول لاحقاً.")
        return

    db.clone_save_target(cid, oid, info.message_id, user.id)
    db.clone_save_target(cid, oid, content.message_id, user.id)
    await _send_cfg(context.bot, user.id, cid, "ack")


# --- لوحة صاحب البوت ---
async def _show(update, text, markup):
    if update.callback_query:
        await _edit(update.callback_query, text, markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def clone_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show(update, "🛠 لوحة تحكم بوتك:", _panel_markup())


def _gender_menu(bot_id):
    cfg = _gender_cfg(bot_id)
    state = "مفعّلة ✅" if cfg["enabled"] else "معطّلة ⛔"
    text = (
        "🎚 أزرار ولد / أميرة\n\n"
        f"الحالة: {state}\n"
        f"نص الزر الأول: {cfg['male_button']}\n"
        f"نص الزر الثاني: {cfg['female_button']}\n"
        f"عنوان رسالة الأول: {cfg['male_label']}\n"
        f"عنوان رسالة الثاني: {cfg['female_label']}\n\n"
        "عند التعطيل يرسل الأعضاء رسائلهم مباشرة دون اختيار."
    )
    toggle = "⛔ تعطيل الأزرار" if cfg["enabled"] else "✅ تفعيل الأزرار"
    markup = InlineKeyboardMarkup([
        [_btn(toggle, "o_gtoggle", "success")],
        [_btn("✏️ نص الزر الأول", "o_gmale"), _btn("✏️ نص الزر الثاني", "o_gfemale")],
        [_btn("✏️ عنوان رسالة الأول", "o_lmale"), _btn("✏️ عنوان رسالة الثاني", "o_lfemale")],
        [_btn("🔙 رجوع", "o_panel")],
    ])
    return text, markup


_EXPECT_PROMPTS = {
    "welcome": "أرسل نص رسالة الترحيب الجديدة (يدعم التنسيق).",
    "ack": "أرسل نص رسالة الإرسال الجديدة (تصل للعضو بعد كل رسالة يرسلها).",
    "male_button": "أرسل النص الجديد لزر الاختيار الأول (الولد).",
    "female_button": "أرسل النص الجديد لزر الاختيار الثاني (الأميرة).",
    "male_label": "أرسل العنوان الجديد الذي يظهر لك فوق رسائل الأول (مثال: رسالة الولد).",
    "female_label": "أرسل العنوان الجديد الذي يظهر لك فوق رسائل الثاني (مثال: رسالة الأميرة).",
}
_CALLBACK_TO_EXPECT = {
    "o_welcome": "welcome", "o_ack": "ack",
    "o_gmale": "male_button", "o_gfemale": "female_button",
    "o_lmale": "male_label", "o_lfemale": "female_label",
}


async def clone_owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cid, oid = _cid(context), _oid(context)
    if update.effective_user.id != oid:
        await query.answer("هذه اللوحة لصاحب البوت فقط.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    context.user_data.pop("o_expect", None)

    if data == "o_panel":
        await _edit(query, "🛠 لوحة تحكم بوتك:", _panel_markup())
    elif data in _CALLBACK_TO_EXPECT:
        expect = _CALLBACK_TO_EXPECT[data]
        context.user_data["o_expect"] = expect
        extra = "\nأرسل «افتراضي» لاسترجاع الكليشة الافتراضية." if expect in DEFAULTS else ""
        back = "o_panel" if expect in DEFAULTS else "o_gender"
        await _edit(
            query, _EXPECT_PROMPTS[expect] + extra + "\nللإلغاء أرسل «إلغاء».",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", back)]]),
        )
    elif data == "o_gender":
        text, markup = _gender_menu(cid)
        await _edit(query, text, markup)
    elif data == "o_gtoggle":
        enabled = _gender_cfg(cid)["enabled"]
        db.clone_set_setting(cid, "gender_enabled", "0" if enabled else "1")
        text, markup = _gender_menu(cid)
        await _edit(query, text, markup)
    elif data == "o_bc":
        context.user_data["o_expect"] = "broadcast"
        await _edit(
            query,
            "📢 أرسل الرسالة التي تريد إذاعتها لأعضاء بوتك (أي نوع: نص، صورة، فيديو...).\n"
            "للإلغاء أرسل «إلغاء».",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", "o_panel")]]),
        )
    elif data == "o_stats":
        members, banned, total = db.clone_stats(cid)
        await _edit(
            query,
            f"📊 إحصائيات بوتك\n👥 الأعضاء: {members}\n🚫 المحظورون: {banned}\n✉️ الرسائل: {total}",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", "o_panel")]]),
        )
    elif data == "o_help":
        await _edit(
            query,
            "🚫 الحظر وإلغاء الحظر\n\n"
            "• ردّ على رسالة العضو واكتب: حظر  /  إلغاء الحظر\n"
            "• أو بالأيدي: حظر 123456789\n"
            "• أو باليوزر: حظر @username  /  إلغاء الحظر @username\n\n"
            "يعمل أيضاً /ban و /unban.",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", "o_panel")]]),
        )


async def _clone_broadcast(bot, cid, owner_id, from_chat, message_id):
    ids = db.clone_user_ids(cid, exclude_user_id=owner_id)
    ok = 0
    for uid in ids:
        if await _copy_with_retry(bot, uid, from_chat, message_id):
            ok += 1
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(owner_id, f"✅ تمت الإذاعة: وصلت إلى {ok} من {len(ids)} عضو.")
    except Exception:
        pass


async def _owner_expect(update, context, expect):
    msg = update.message
    cid, oid = _cid(context), _oid(context)

    if expect == "broadcast":
        context.user_data.pop("o_expect", None)
        await msg.reply_text("⏳ بدأت الإذاعة، سيصلك تقرير عند الانتهاء.")
        context.application.create_task(
            _clone_broadcast(context.bot, cid, oid, msg.chat_id, msg.message_id)
        )
        return

    if msg.text is None:
        await msg.reply_text("أرسل نصاً، أو أرسل «إلغاء».")
        return
    text = msg.text.strip()

    if expect in DEFAULTS:
        if text in RESET_WORDS:
            db.clone_del_setting(cid, f"{expect}_message", f"{expect}_entities")
            await msg.reply_text("✅ رجعت الرسالة للكليشة الافتراضية.")
        else:
            db.clone_set_setting(cid, f"{expect}_message", msg.text)
            db.clone_set_setting(cid, f"{expect}_entities", _entities_json(msg))
            await msg.reply_text("✅ تم الحفظ. هذه الرسالة صارت خاصة ببوتك ولن تتأثر بتغيير المالك.")
    elif expect in GENDER_DEFAULTS:
        if len(text) > 40:
            await msg.reply_text("النص طويل (الحد 40 حرفاً). أرسل نصاً أقصر.")
            return
        db.clone_set_setting(cid, expect, text)
        await msg.reply_text("✅ تم الحفظ.")
    context.user_data.pop("o_expect", None)


# --- الحظر ---
def _parse_moderation(text):
    m = UNBAN_RE.match(text)
    if m:
        return "unban", m.group(1)
    m = BAN_RE.match(text)
    if m:
        return "ban", m.group(1)
    return None, None


async def _moderate(update, context, action, arg):
    msg = update.message
    cid, oid = _cid(context), _oid(context)
    target = None

    if arg:
        if arg.lstrip("-").isdigit():
            target = int(arg)
            if db.clone_get_user(cid, target) is None:
                db.clone_ensure_user(cid, target)
        else:
            target = db.clone_find_user_by_username(cid, arg)
            if target is None:
                await msg.reply_text("❌ لم أجد هذا اليوزر بين أعضاء بوتك. جرّب الأيدي أو الرد على رسالته.")
                return
    elif msg.reply_to_message:
        target = db.clone_get_target(cid, msg.chat_id, msg.reply_to_message.message_id)
        if target is None:
            await msg.reply_text("❌ ردّ على رسالة عضو وصلتك من البوت (بطاقته أو محتوى رسالته).")
            return
    else:
        await msg.reply_text("ردّ على رسالة العضو، أو اكتب: حظر 123456 / حظر @username")
        return

    if target == oid:
        await msg.reply_text("لا يمكنك حظر نفسك 🙂")
        return
    db.clone_set_ban(cid, target, action == "ban")
    await msg.reply_text(
        f"✅ تم حظر المستخدم {target}." if action == "ban" else f"✅ تم رفع الحظر عن {target}."
    )


async def clone_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0] if context.args else None
    await _moderate(update, context, "ban", arg)


async def clone_unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = context.args[0] if context.args else None
    await _moderate(update, context, "unban", arg)


async def clone_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    cid = _cid(context)
    text = (msg.text or "").strip()

    expect = context.user_data.get("o_expect")
    if expect:
        if text in CANCEL_WORDS:
            context.user_data.pop("o_expect", None)
            await msg.reply_text("تم الإلغاء.")
            return
        await _owner_expect(update, context, expect)
        return

    if text and PANEL_RE.match(text):
        await clone_panel(update, context)
        return

    if text:
        action, arg = _parse_moderation(text)
        if action:
            await _moderate(update, context, action, arg)
            return

    if msg.reply_to_message:
        target = db.clone_get_target(cid, msg.chat_id, msg.reply_to_message.message_id)
        if target is None:
            await msg.reply_text("❌ ردّ على رسالة عضو وصلتك من البوت حتى يصله ردك.")
            return
        try:
            if msg.text is not None:
                await _send_text_entities(
                    context.bot, target, msg.text, list(msg.entities) or None
                )
            else:
                await context.bot.copy_message(
                    chat_id=target, from_chat_id=msg.chat_id, message_id=msg.message_id
                )
        except Exception:
            await msg.reply_text("❌ تعذّر إرسال الرد (ربما أوقف العضو البوت).")
            return
        await msg.reply_text("✅ تم إرسال الرد.")


def _build_clone_app(bot_id, token, owner_id):
    app = Application.builder().token(token).job_queue(None).build()
    app.bot_data["clone_id"] = bot_id
    app.bot_data["clone_owner"] = owner_id

    private = filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE
    owner = filters.User(owner_id)
    plain = ~filters.COMMAND & ~filters.StatusUpdate.ALL

    app.add_handler(CommandHandler("start", clone_start, filters=private))
    app.add_handler(CommandHandler(["admin", "panel"], clone_panel, filters=private & owner))
    app.add_handler(CommandHandler("ban", clone_ban_command, filters=private & owner))
    app.add_handler(CommandHandler("unban", clone_unban_command, filters=private & owner))
    app.add_handler(CallbackQueryHandler(clone_set_gender, pattern="^set_gender:"))
    app.add_handler(CallbackQueryHandler(clone_owner_callback, pattern="^o_"))
    app.add_handler(MessageHandler(private & owner & plain, clone_owner_message))
    app.add_handler(MessageHandler(private & ~owner & plain, clone_user_message))
    app.add_error_handler(_clone_error)
    return app
