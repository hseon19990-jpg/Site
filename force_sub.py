"""الاشتراك الإجباري + تتبّع القنوات والجروبات (للإذاعة).

أنواع الاشتراك (scope):
  main   : إجباري خاص بالمالك — يُطبَّق على البوت الرئيسي فقط.
  global : إجباري للكل — يُطبَّق على البوت الرئيسي وعلى كل البوتات المصنوعة.
  clone  : إجباري يضعه صانع البوت — يُطبَّق على بوته فقط.

كل اشتراك قد يكون قناة أو جروب أو بوت. ويمكن وضع أكثر من اشتراك.

التحقق:
  • القنوات والجروبات: getChatMember، ويشترط أن يكون البوت المُدقِّق مشرفاً هناك.
      - main/global يدقّقها البوت الرئيسي (لذلك يجب أن يكون مشرفاً فيها)،
        فلا يلزم أن تكون كل البوتات المصنوعة مشرفة في القناة.
      - clone يدقّقها بوت الصانع نفسه (يجب أن يكون مشرفاً فيها).
  • البوتات: تيليجرام لا يخبر البوت إن كان شخص ما ضغط Start في بوت آخر. لذلك نتحقق
    فقط إذا كان البوت المطلوب هو البوت الرئيسي أو أحد البوتات المصنوعة (من قاعدة بياناتنا).
    أما البوت الخارجي فيُعرض كرابط ولا يمكن إلزامه فعلياً.
"""
import asyncio
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ChatMemberHandler, MessageHandler, filters

import config
import controls
import database as db

logger = logging.getLogger(__name__)

MAIN_BOT = None          # يُضبط عند تسجيل معالجات البوت الرئيسي
MAX_MAIN_SUBS = 10       # حد اشتراكات المالك (main + global)
MAX_CLONE_SUBS = 5       # حد اشتراكات كل صانع بوت

ICONS = {"channel": "📣", "group": "👥", "bot": "🤖"}
KIND_NAMES = {"channel": "قناة", "group": "جروب", "bot": "بوت"}

_OK_TTL = 60             # ثواني نحتفظ فيها بنتيجة "مشترك" لتخفيف الطلبات
_OK_CACHE: dict = {}     # (user_id, chat_id) -> وقت الانتهاء
_SEEN_CHATS: set = set()  # (bot_key, chat_id) التي سُجلت أثناء هذه الجلسة


def set_main_bot(bot):
    global MAIN_BOT
    MAIN_BOT = bot


# ---------------------------------------------------------------------------
# أدوات
# ---------------------------------------------------------------------------
def _ctx_ids(context):
    """(bot_key, exempt_id): bot_key = 0 للرئيسي أو bot_id للمصنوع."""
    data = context.application.bot_data
    if "clone_id" in data:
        return data["clone_id"], data["clone_owner"]
    return 0, config.OWNER_ID


def _oid_of(bot_id):
    clone = db.get_clone(bot_id)
    return clone["owner_id"] if clone else None


def _flag(value, default=True):
    if value is None or value == "":
        return default
    return value == "1"


def main_enabled(scope):
    return _flag(db.get_setting(f"fs_{scope}_enabled"))


def clone_enabled(bot_id):
    return _flag(db.clone_get_setting(bot_id, "fs_enabled"))


def applicable_subs(bot_key):
    """الاشتراكات المطبّقة الآن على البوت (بعد احتساب المفتاح مفعّل/مغلق)."""
    subs = []
    if bot_key == 0:
        if main_enabled("main"):
            subs += db.list_force_subs(["main"])
    if main_enabled("global"):
        subs += db.list_force_subs(["global"])
    if bot_key != 0 and clone_enabled(bot_key):
        subs += db.list_force_subs(["clone"], bot_key)
    return subs


def parse_ref(text):
    """@user أو t.me/user أو أيدي رقمي. يرجع None إن لم يُفهم."""
    t = (text or "").strip()
    m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,})/?", t)
    if m:
        return "@" + m.group(1)
    m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,})", t)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"-?\d{5,}", t):
        return int(t)
    return None


async def resolve(bot, text):
    """يتحقق من المعرّف ويرجع (بيانات الاشتراك، None) أو (None، رسالة خطأ)."""
    ref = parse_ref(text)
    if ref is None:
        return None, (
            "❌ لم أفهم المعرّف. أرسل @username أو رابط t.me/username أو الأيدي الرقمي "
            "(مثل -1001234567890).\nروابط الدعوة الخاصة (t.me/+...) غير مدعومة هنا؛ "
            "أرسل الأيدي الرقمي بدلاً منها."
        )
    try:
        chat = await bot.get_chat(ref)
    except (BadRequest, Forbidden):
        return None, "❌ لم أجد هذا المعرّف. تأكد منه، وإن كانت قناة/جروب خاص فأضف البوت إليه أولاً."
    except TelegramError:
        return None, "⚠️ تعذّر الوصول إلى تيليجرام الآن، حاول بعد قليل."

    if chat.type == "channel":
        kind = "channel"
    elif chat.type in ("group", "supergroup"):
        kind = "group"
    elif chat.type == "private" and (chat.username or "").lower().endswith("bot"):
        kind = "bot"
    else:
        return None, "❌ يمكن فقط وضع قناة أو جروب أو بوت."

    title = chat.title or chat.full_name or chat.username or str(chat.id)
    if chat.id == bot.id:
        return None, "❌ لا يمكن جعل البوت نفسه اشتراكاً إجبارياً."

    if kind == "bot":
        link = f"https://t.me/{chat.username}"
    else:
        try:
            me = await bot.get_chat_member(chat.id, bot.id)
        except TelegramError:
            me = None
        if me is None or me.status not in ("administrator", "creator"):
            return None, (
                f"❌ البوت (@{bot.username}) ليس مشرفاً في هذه {KIND_NAMES[kind]}.\n"
                "ارفعه مشرفاً أولاً (يحتاج صلاحية رؤية الأعضاء) ثم أعد إرسال المعرّف."
            )
        link = f"https://t.me/{chat.username}" if chat.username else getattr(chat, "invite_link", None)
        if not link:
            try:
                link = await bot.export_chat_invite_link(chat.id)
            except TelegramError:
                link = None
        if not link:
            return None, (
                f"❌ هذه {KIND_NAMES[kind]} خاصة ولا أستطيع إنشاء رابط دعوة لها. "
                "أعطِ البوت صلاحية «دعوة المستخدمين عبر الرابط» أو اجعلها عامة بيوزر."
            )

    return {
        "kind": kind, "chat_id": chat.id, "username": chat.username,
        "title": title, "link": link,
    }, None


# ---------------------------------------------------------------------------
# التحقق من اشتراك مستخدم
# ---------------------------------------------------------------------------
def _bot_started(sub, user_id, current_bot_id):
    """True/False إن كان البوت المطلوب بوتاً تابعاً للسايت، وإلا None (لا يمكن التحقق)."""
    bid = sub["chat_id"]
    if bid == current_bot_id:
        return True
    try:
        main_id = MAIN_BOT.id if MAIN_BOT is not None else None
    except Exception:
        main_id = None
    if bid == main_id:
        return db.get_user(user_id) is not None
    if db.get_clone(bid) is not None:
        return db.clone_get_user(bid, user_id) is not None
    return None


async def _is_member(checker, sub, user_id, current_bot_id):
    """True مشترك، False غير مشترك، None لا يمكن التحقق (لا نمنع المستخدم)."""
    if sub["kind"] == "bot":
        return _bot_started(sub, user_id, current_bot_id)

    key = (user_id, sub["chat_id"])
    if _OK_CACHE.get(key, 0) > time.time():
        return True
    try:
        member = await checker.get_chat_member(sub["chat_id"], user_id)
    except Exception as exc:
        # البوت ليس مشرفاً / القناة حُذفت / مشكلة شبكة: لا نحجب المستخدمين بسبب خلل إعداد.
        logger.warning("force-sub check failed for %s: %s", sub["chat_id"], type(exc).__name__)
        return None
    if member.status in ("left", "kicked"):
        return False
    if member.status == "restricted" and getattr(member, "is_member", True) is False:
        return False
    _OK_CACHE[key] = time.time() + _OK_TTL
    return True


async def find_missing(bot_key, current_bot, user_id):
    subs = applicable_subs(bot_key)
    if not subs:
        return []
    results = await asyncio.gather(*(
        _is_member(
            current_bot if (s["scope"] == "clone" or MAIN_BOT is None) else MAIN_BOT,
            s, user_id, current_bot.id,
        )
        for s in subs
    ))
    return [s for s, ok in zip(subs, results) if ok is False]


def _gate_markup(missing):
    rows = [
        [InlineKeyboardButton(f"{ICONS[s['kind']]} {s['title']}", url=s["link"])]
        for s in missing if s.get("link")
    ]
    rows.append([InlineKeyboardButton("✅ تحققت من اشتراكي", callback_data="fs_verify")])
    return InlineKeyboardMarkup(rows)


async def gate_user(context, user, chat_id=None):
    """True إذا يحق للمستخدم المتابعة؛ وإلا يرسل له رسالة الاشتراك ويرجع False."""
    bot_key, exempt = _ctx_ids(context)
    if user is None or user.id == exempt:
        return True
    if not await controls.guard(context, user, chat_id, bot_key):
        return False
    missing = await find_missing(bot_key, context.bot, user.id)
    if not missing:
        return True
    try:
        await context.bot.send_message(
            chat_id or user.id,
            "🔒 للاستخدام يجب الاشتراك أولاً في الأماكن التالية، ثم اضغط «تحققت من اشتراكي»:",
            reply_markup=_gate_markup(missing),
        )
    except Exception:
        logger.warning("could not send force-sub gate message")
    return False


async def gate(update, context):
    chat = update.effective_chat
    return await gate_user(context, update.effective_user, chat.id if chat else None)


async def verify_callback(update, context):
    query = update.callback_query
    user = update.effective_user
    bot_key, exempt = _ctx_ids(context)
    for key in [k for k in _OK_CACHE if k[0] == user.id]:
        _OK_CACHE.pop(key, None)
    missing = [] if user.id == exempt else await find_missing(bot_key, context.bot, user.id)
    if missing:
        names = "، ".join(s["title"] for s in missing)
        await query.answer(f"لم يكتمل اشتراكك بعد ❌\nالناقص: {names}"[:190], show_alert=True)
        try:
            await query.edit_message_reply_markup(_gate_markup(missing))
        except Exception:
            pass
        return
    await query.answer("✅ تم التحقق")
    try:
        await query.edit_message_text("✅ تم التحقق من اشتراكك. أرسل /start أو رسالتك الآن.")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# تتبّع القنوات والجروبات التي يعمل فيها كل بوت (للإذاعة)
# ---------------------------------------------------------------------------
async def track_my_chat_member(update, context):
    event = update.my_chat_member
    if event is None or event.chat.type == "private":
        return
    chat = event.chat
    bot_key, _ = _ctx_ids(context)
    new = event.new_chat_member
    if new.status in ("left", "kicked"):
        db.delete_known_chat(bot_key, chat.id)
        _SEEN_CHATS.discard((bot_key, chat.id))
        return
    if chat.type == "channel":
        can_post = new.status == "administrator" and bool(getattr(new, "can_post_messages", False))
    elif new.status == "restricted":
        can_post = bool(getattr(new, "can_send_messages", False))
    else:
        can_post = True
    db.upsert_known_chat(bot_key, chat.id, chat.type, chat.title or str(chat.id), can_post)
    _SEEN_CHATS.add((bot_key, chat.id))


async def track_chat_activity(update, context):
    """يلتقط القنوات/الجروبات التي كان البوت فيها قبل هذا التحديث (أول رسالة تصله منها)."""
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        return
    bot_key, _ = _ctx_ids(context)
    if (bot_key, chat.id) in _SEEN_CHATS:
        return
    _SEEN_CHATS.add((bot_key, chat.id))
    db.ensure_known_chat(bot_key, chat.id, chat.type, chat.title or str(chat.id))


def register_chat_tracking(app):
    app.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=5)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS | filters.ChatType.CHANNEL, track_chat_activity),
        group=5,
    )


# ---------------------------------------------------------------------------
# واجهة التحكم
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


def _state(on):
    return "مفعّل ✅" if on else "مغلق ⛔ (مفتوح للجميع)"


def _lines(subs):
    if not subs:
        return ["   — لا يوجد —"]
    return [f"   {i}) {ICONS[s['kind']]} {KIND_NAMES[s['kind']]}: {s['title']}" for i, s in enumerate(subs, 1)]


_BOT_NOTE = (
    "ℹ️ الإلزام بـ«بوت» يعمل فقط إذا كان بوتاً تابعاً للسايت. البوت الخارجي يظهر كرابط ولا يمكن التحقق منه."
)


# ---- المالك (البوت الرئيسي) ----
def main_menu():
    own = db.list_force_subs(["main"])
    glob = db.list_force_subs(["global"])
    text = "\n".join([
        "🔒 الاشتراك الإجباري",
        "",
        f"👑 إجباري خاص بي (بوتي فقط) — {_state(main_enabled('main'))}",
        *_lines(own),
        "",
        f"🌐 إجباري للكل (بوتي + كل البوتات المصنوعة) — {_state(main_enabled('global'))}",
        *_lines(glob),
        "",
        "الشرط: بوتي الرئيسي يكون مشرفاً في القناة/الجروب.",
        _BOT_NOTE,
    ])
    rows = [
        [_btn("➕ إجباري لي", "cl_fs_add:main", "success"),
         _btn("➕ إجباري للكل", "cl_fs_add:global", "success")],
        [_btn("⏯ تشغيل/إيقاف: لي", "cl_fs_tog:main"),
         _btn("⏯ تشغيل/إيقاف: للكل", "cl_fs_tog:global")],
    ]
    for s in own + glob:
        tag = "👑" if s["scope"] == "main" else "🌐"
        rows.append([_btn(f"🗑 {tag} {s['title']}"[:60], f"cl_fs_del:{s['id']}", "danger")])
    rows.append([_btn("🔙 رجوع", "settings")])
    return text, InlineKeyboardMarkup(rows)


async def main_callback(update, context):
    query = update.callback_query
    if update.effective_user.id != config.OWNER_ID:
        await query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
        return
    data = query.data or ""
    action, _, arg = data.partition(":")

    if action == "cl_fs":
        await query.answer()
        context.user_data.pop("cl_expect", None)
    elif action == "cl_fs_add" and arg in ("main", "global"):
        if len(db.list_force_subs(["main", "global"])) >= MAX_MAIN_SUBS:
            await query.answer(f"الحد الأقصى {MAX_MAIN_SUBS} اشتراكات.", show_alert=True)
            return
        await query.answer()
        context.user_data["cl_expect"] = f"fs_{arg}"
        where = "بوتك الرئيسي فقط" if arg == "main" else "كل البوتات (الرئيسي والمصنوعة)"
        await _edit(
            query,
            f"أرسل معرّف القناة أو الجروب أو البوت الذي تريده إجبارياً على {where}.\n"
            "مثال: @channel أو t.me/channel أو -1001234567890\n\n"
            "بوتك الرئيسي يجب أن يكون مشرفاً فيها. للإلغاء أرسل «إلغاء».",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", "cl_fs")]]),
        )
        return
    elif action == "cl_fs_tog" and arg in ("main", "global"):
        await query.answer()
        db.set_setting(f"fs_{arg}_enabled", "0" if main_enabled(arg) else "1")
    elif action == "cl_fs_del":
        await query.answer()
        sub = db.get_force_sub(int(arg or 0))
        if sub and sub["scope"] in ("main", "global"):
            db.delete_force_sub(sub["id"])
            db.log_activity("fs_del_" + sub["scope"], config.OWNER_ID, 0, sub["title"])
    else:
        await query.answer()
    text, markup = main_menu()
    await _edit(query, text, markup)


async def main_add_input(update, context, scope):
    """يستقبل معرّف الاشتراك من المالك. يرجع True إذا اكتمل الإدخال."""
    msg = update.message
    if not msg.text:
        await msg.reply_text("أرسل المعرّف كنص، أو «إلغاء».")
        return False
    sub, err = await resolve(context.bot, msg.text)
    if err:
        await msg.reply_text(err + "\n\nأعد المحاولة أو أرسل «إلغاء».")
        return False
    if len(db.list_force_subs(["main", "global"])) >= MAX_MAIN_SUBS:
        await msg.reply_text(f"❌ وصلت للحد الأقصى ({MAX_MAIN_SUBS}).")
        return True
    added = db.add_force_sub(scope, 0, sub["kind"], sub["chat_id"], sub["username"], sub["title"], sub["link"])
    if added:
        db.log_activity(f"fs_add_{scope}", config.OWNER_ID, 0, sub["title"])
    note = ""
    if sub["kind"] == "bot" and db.get_clone(sub["chat_id"]) is None and (
        MAIN_BOT is None or sub["chat_id"] != MAIN_BOT.id
    ):
        note = "\n\n⚠️ هذا بوت خارجي: سيظهر كرابط فقط ولا يمكن التحقق من ضغط Start فيه."
    await msg.reply_text(
        (f"✅ تمت إضافة {KIND_NAMES[sub['kind']]}: {sub['title']}" if added else "ℹ️ هذا الاشتراك موجود مسبقاً.") + note,
        reply_markup=InlineKeyboardMarkup([[_btn("🔒 قائمة الاشتراك الإجباري", "cl_fs")]]),
    )
    return True


# ---- صانع البوت (بوت مصنوع) ----
def clone_menu(bot_id):
    own = db.list_force_subs(["clone"], bot_id)
    glob = db.list_force_subs(["global"]) if main_enabled("global") else []
    lines = [
        "🔒 الاشتراك الإجباري لبوتك",
        "",
        f"اشتراكاتك — {_state(clone_enabled(bot_id))}",
        *_lines(own),
    ]
    if glob:
        lines += ["", f"🌐 مفروضة من إدارة السايت على كل البوتات: {len(glob)} (لا يمكن تعديلها)"]
    lines += ["", "الشرط: بوتك يكون مشرفاً في القناة/الجروب المطلوب.", _BOT_NOTE]
    rows = [
        [_btn("➕ إضافة اشتراك", "o_fs_add", "success")],
        [_btn("⏹ إيقاف الإجباري" if clone_enabled(bot_id) else "▶️ تشغيل الإجباري", "o_fs_tog")],
    ]
    for s in own:
        rows.append([_btn(f"🗑 {s['title']}"[:60], f"o_fs_del:{s['id']}", "danger")])
    rows.append([_btn("🔙 رجوع", "o_panel")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def clone_callback(update, context, bot_id):
    """يُستدعى من clone_owner_callback لكل زر يبدأ بـ o_fs (بعد التحقق أنه صاحب البوت)."""
    query = update.callback_query
    action, _, arg = (query.data or "").partition(":")
    if action == "o_fs_add":
        if len(db.list_force_subs(["clone"], bot_id)) >= MAX_CLONE_SUBS:
            await query.answer(f"الحد الأقصى {MAX_CLONE_SUBS} اشتراكات.", show_alert=True)
            return
        context.user_data["o_expect"] = "fs_add"
        await _edit(
            query,
            "أرسل معرّف القناة أو الجروب أو البوت الذي تريده إجبارياً.\n"
            "مثال: @channel أو t.me/channel أو -1001234567890\n\n"
            "⚠️ يجب أن يكون بوتك مشرفاً في القناة/الجروب حتى يتحقق من المشتركين.\n"
            "للإلغاء أرسل «إلغاء».",
            InlineKeyboardMarkup([[_btn("🔙 رجوع", "o_fs")]]),
        )
        return
    if action == "o_fs_tog":
        db.clone_set_setting(bot_id, "fs_enabled", "0" if clone_enabled(bot_id) else "1")
    elif action == "o_fs_del":
        sub = db.get_force_sub(int(arg or 0))
        if sub and sub["scope"] == "clone" and sub["bot_id"] == bot_id:
            db.delete_force_sub(sub["id"])
    text, markup = clone_menu(bot_id)
    await _edit(query, text, markup)


async def clone_add_input(update, context, bot_id):
    """يستقبل معرّف الاشتراك من صانع البوت. يرجع True إذا اكتمل الإدخال."""
    msg = update.message
    if not msg.text:
        await msg.reply_text("أرسل المعرّف كنص، أو «إلغاء».")
        return False
    sub, err = await resolve(context.bot, msg.text)
    if err:
        await msg.reply_text(err + "\n\nأعد المحاولة أو أرسل «إلغاء».")
        return False
    if len(db.list_force_subs(["clone"], bot_id)) >= MAX_CLONE_SUBS:
        await msg.reply_text(f"❌ وصلت للحد الأقصى ({MAX_CLONE_SUBS}).")
        return True
    added = db.add_force_sub("clone", bot_id, sub["kind"], sub["chat_id"], sub["username"], sub["title"], sub["link"])
    if added:
        db.log_activity("fs_add_clone", _oid_of(bot_id), bot_id, sub["title"])
    note = ""
    if sub["kind"] == "bot" and db.get_clone(sub["chat_id"]) is None and (
        MAIN_BOT is None or sub["chat_id"] != MAIN_BOT.id
    ):
        note = "\n\n⚠️ هذا بوت خارجي: سيظهر كرابط فقط ولا يمكن التحقق من ضغط Start فيه."
    await msg.reply_text(
        (f"✅ تمت إضافة {KIND_NAMES[sub['kind']]}: {sub['title']}" if added else "ℹ️ هذا الاشتراك موجود مسبقاً.") + note,
        reply_markup=InlineKeyboardMarkup([[_btn("🔒 قائمة الاشتراك", "o_fs")]]),
    )
    return True
