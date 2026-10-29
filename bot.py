import logging
import json
import re
from html import escape as html_escape
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
    ReplyParameters,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode
import config
from database import (
    init_db, get_setting, set_setting, add_user, increment_message_count,
    get_user, set_user_status, get_all_users, get_user_gender,
    set_user_gender, save_message_target, get_message_target,
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BUTTON_ICON_LABELS = {
    "admin": "لوحة الإدارة",
    "settings": "الإعدادات",
    "moderation": "إدارة المستخدمين",
    "stats": "الإحصائيات",
    "broadcast": "البث",
    "welcome": "تغيير رسالة الترحيب",
    "ack": "تغيير رسالة التأكيد",
    "back": "رجوع",
}


def admin_button(
    text: str,
    callback_data: str,
    style: str = "primary",
    icon_key=None,
) -> InlineKeyboardButton:
    """إنشاء زر لوحة الإدارة مع لون Telegram وأيقونة Premium اختيارية."""
    kwargs = {"style": style}
    if icon_key:
        icon_id = get_setting(f"button_icon_{icon_key}")
        if icon_id:
            kwargs["icon_custom_emoji_id"] = icon_id
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def extract_target_user_id(message_text: str) -> int:
    """استخراج معرّف المستخدم بعد إزالة Telegram لوسوم التنسيق."""
    match = re.search(
        r"(?m)^\s*🆔\s*(?:الأيدي|ID)\s*:\s*`?(\d+)`?\s*$",
        message_text or "",
    )
    if not match:
        raise ValueError("لم يتم العثور على أيدي المستخدم في الرسالة.")
    return int(match.group(1))


def get_target_user_id_from_message(message) -> int:
    """استخراج المستخدم من بطاقة المعلومات أو من رسالة المحتوى المرتبطة بها."""
    try:
        return extract_target_user_id(message.text or message.caption or "")
    except ValueError:
        target_id = get_message_target(message.chat_id, message.message_id)
        if target_id is None:
            raise
        return target_id


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("ذكر", callback_data="set_gender:male"),
        InlineKeyboardButton("أنثى", callback_data="set_gender:female"),
    ]])


async def prompt_for_gender(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=chat_id,
        text="قبل إرسال الرسائل، اختر الجنس من الأزرار:",
        reply_markup=gender_keyboard(),
    )


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.OWNER_ID


async def reject_non_owner_callback(update: Update) -> bool:
    if is_owner(update):
        return False
    await update.callback_query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
    return True


async def send_configured_message(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    message_key: str,
    reply_markup=None,
):
    """إرسال النص بتنسيقه المحفوظ ثم إعادة توجيه ملصقه إن وجد."""
    text = get_setting(f"{message_key}_message") or ""
    raw_entities = get_setting(f"{message_key}_entities")
    sticker_chat_id = get_setting(f"{message_key}_sticker_chat_id")
    sticker_message_id = get_setting(f"{message_key}_sticker_message_id")

    if raw_entities is None and text:
        # تنسيق أوضح للرسائل القديمة التي لم تكن تحفظ كيانات التنسيق.
        text_length = len(text.encode("utf-16-le")) // 2
        entities = [
            MessageEntity(
                type=MessageEntity.BOLD,
                offset=0,
                length=text_length,
            )
        ]
    else:
        try:
            saved_entities = json.loads(raw_entities or "[]")
            if not isinstance(saved_entities, list):
                raise ValueError("قائمة الكيانات المحفوظة غير صالحة")
            entities = [
                MessageEntity.de_json(entity, context.bot)
                for entity in saved_entities
            ]
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("تنسيق محفوظ غير صالح للرسالة الثابتة %s", message_key)
            entities = []

    if text:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities or None,
                reply_markup=reply_markup,
            )
        except BadRequest:
            if not entities:
                raise
            logger.warning(
                "تعذر إرسال تنسيق الرسالة %s؛ سيُرسل النص دون تنسيق",
                message_key,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
            )
    elif reply_markup:
        await context.bot.send_message(
            chat_id=chat_id,
            text="لوحة تحكم المالك:",
            reply_markup=reply_markup,
        )

    if sticker_chat_id and sticker_message_id:
        try:
            await context.bot.forward_message(
                chat_id=chat_id,
                from_chat_id=int(sticker_chat_id),
                message_id=int(sticker_message_id),
            )
        except Exception:
            logger.exception("تعذّر إعادة توجيه ملصق الرسالة الثابتة %s", message_key)
            await context.bot.send_message(
                chat_id=chat_id,
                text="تعذّر إرسال الملصق المحفوظ.",
            )


async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """التحقق من الاشتراك في القناة والبوت الإجباري"""
    user_id = update.effective_user.id
    force_channel = get_setting("force_channel")
    force_bot = get_setting("force_bot")
    
    if force_channel:
        try:
            member = await context.bot.get_chat_member(chat_id=force_channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                await update.message.reply_text(f"عذراً، يجب عليك الاشتراك في القناة أولاً:\n{force_channel}")
                return False
        except Exception:
            pass
    
    if force_bot:
        # التحقق من بدء البوت الإجباري (يمكن تنفيذه عبر getChat أو إرسال رسالة تجريبية)
        try:
            await context.bot.get_chat(force_bot)
        except Exception:
            pass
    
    return True

# --- أوامر المستخدم ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.full_name, user.username)
    
    if not await is_subscribed(update, context):
        return
    
    reply_markup = None
    if is_owner(update):
        reply_markup = InlineKeyboardMarkup([[
            admin_button(
                "👑 لوحة الإدارة",
                callback_data="open_admin_panel",
                icon_key="admin",
            )
        ]])
    await send_configured_message(user.id, context, "welcome", reply_markup)
    if not is_owner(update) and get_user_gender(user.id) is None:
        await prompt_for_gender(user.id, context)


async def set_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    gender = (query.data or "").partition(":")[2]
    if user is None or gender not in {"male", "female"}:
        await query.answer("الاختيار غير صالح.", show_alert=True)
        return

    add_user(user.id, user.full_name, user.username)
    set_user_gender(user.id, gender)
    await query.answer("تم حفظ اختيارك.")
    await context.bot.send_message(
        chat_id=user.id,
        text="تم حفظ اختيارك. يمكنك الآن إرسال رسالتك.",
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.full_name, user.username)
    
    if not await is_subscribed(update, context):
        return
    
    user_data = get_user(user.id)
    if user_data and user_data[4] == 1:  # is_banned
        await update.message.reply_text("أنت محظور من استخدام البوت.")
        return
    if user_data and user_data[5] == 1:  # is_restricted
        await update.message.reply_text("أنت مقيد من إرسال الرسائل.")
        return

    gender = get_user_gender(user.id)
    if gender not in {"male", "female"}:
        await update.message.reply_text(
            "اختر الجنس أولاً من الأزرار، ثم أعد إرسال رسالتك:",
            reply_markup=gender_keyboard(),
        )
        return
    
    increment_message_count(user.id)
    count = get_user(user.id)[3]
    
    # استخدم HTML مع تهريب النصوص حتى يبقى الاسم رابطاً آمناً والرسالة كما كتبها المستخدم.
    owner_msg = f"🆔 الأيدي: <code>{user.id}</code>\n"
    owner_msg += (
        f'📩 رسالة جديدة من: <a href="tg://openmessage?user_id={user.id}">'
        f"{html_escape(user.full_name)}</a>\n"
    )
    if user.username:
        owner_msg += f"👤 اسم المستخدم: @{html_escape(user.username)}\n"
    owner_msg += f"📊 عدد الرسائل: {count}\n"
    owner_msg += f"⚧ الجنس: {'ذكر' if gender == 'male' else 'أنثى'}"

    sender_info = await context.bot.send_message(
        chat_id=config.OWNER_ID,
        text=owner_msg,
        parse_mode=ParseMode.HTML,
    )
    gendered_label = "الحلو" if gender == "male" else "الحلوة"
    sender_message = await context.bot.send_message(
        chat_id=config.OWNER_ID,
        text=f"<b>رسالة من {gendered_label}:</b>\n{html_escape(update.message.text)}",
        parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(message_id=sender_info.message_id),
    )
    save_message_target(
        config.OWNER_ID,
        sender_message.message_id,
        user.id,
    )
    
    # إرسال رسالة التأكيد والملصق المحفوظ معها إن وجد.
    await send_configured_message(user.id, context, "ack")

# --- أوامر المالك ---

async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(update):
        if query:
            await query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
        return
    
    keyboard = [
        [admin_button("⚙️ الإعدادات", "settings", icon_key="settings")],
        [admin_button(
            "🛡 إدارة المستخدمين",
            "moderation_help",
            style="success",
            icon_key="moderation",
        )],
        [admin_button(
            "📊 إحصائيات",
            "stats",
            style="success",
            icon_key="stats",
        )],
        [admin_button("📢 إعداد رسالة عامة", "broadcast_setup", icon_key="broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if query:
        await query.answer()
        await query.edit_message_text("لوحة تحكم المالك:", reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text("لوحة تحكم المالك:", reply_markup=reply_markup)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    
    keyboard = [
        [admin_button(
            "✏️ تغيير رسالة الترحيب",
            "edit_welcome",
            icon_key="welcome",
        )],
        [admin_button(
            "✏️ تغيير رسالة التأكيد",
            "edit_ack",
            icon_key="ack",
        )],
        [admin_button("📢 تعيين القناة الإجبارية", "set_channel")],
        [admin_button("🤖 تعيين البوت الإجباري", "set_bot")],
        [admin_button(
            "🎨 تخصيص رموز الأزرار",
            "button_icons",
            style="success",
        )],
        [admin_button("🔙 رجوع", "back_owner", icon_key="back")]
    ]
    await query.edit_message_text("الإعدادات:", reply_markup=InlineKeyboardMarkup(keyboard))


async def button_icons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()

    keyboard = [
        [
            admin_button(
                f"✨ {label}",
                f"set_button_icon:{key}",
                icon_key=key,
            )
        ]
        for key, label in BUTTON_ICON_LABELS.items()
    ]
    keyboard.append([
        admin_button("🔙 رجوع للإعدادات", "settings", icon_key="settings")
    ])
    await query.edit_message_text(
        "اختر الزر الذي تريد تخصيص أيقونته، ثم أرسل إيموجي Premium واحدًا. "
        "لإزالة الأيقونة، أرسل كلمة «حذف».",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_button_icon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return

    target = (query.data or "").partition(":")[2]
    if target not in BUTTON_ICON_LABELS:
        await query.answer("الزر غير معروف.", show_alert=True)
        return

    await query.answer()
    context.user_data["expecting"] = "button_icon"
    context.user_data["button_icon_target"] = target
    await query.edit_message_text(
        f"أرسل إيموجي Premium مخصصًا لأيقونة «{BUTTON_ICON_LABELS[target]}»، "
        "أو أرسل «حذف» لإزالة الأيقونة الحالية."
    )


async def moderation_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    keyboard = [[admin_button("🔙 رجوع", "back_owner", icon_key="back")]]
    text = (
        "🛡 إدارة المستخدمين\n\n"
        "للتحكم بمستخدم، ردّ على إشعار رسالته بأحد الأوامر:\n"
        "/ban — حظر\n"
        "/unban — رفع الحظر\n"
        "/restrict — تقييد\n"
        "/unrestrict — رفع التقييد\n"
        "/stats — عرض الإحصائيات\n\n"
        "هذه الأوامر متاحة للمالك فقط."
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def owner_stats_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    keyboard = [[admin_button("🔙 رجوع", "back_owner", icon_key="back")]]
    await query.edit_message_text(
        f"📊 إحصائيات البوت\nالمستخدمون المسجلون: {len(get_all_users())}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def broadcast_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    context.user_data["expecting"] = "broadcast"
    keyboard = [[admin_button("🔙 رجوع", "back_owner", icon_key="back")]]
    await query.edit_message_text(
        "أرسل نص الرسالة العامة لحفظها. بعد الحفظ، أرسل /broadcast لإرسالها للمستخدمين.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عندما يرد المالك على رسالة، تصل للمستخدم الأصلي"""
    if update.effective_user.id != config.OWNER_ID:
        return

    expecting = context.user_data.get("expecting")
    if expecting and update.message.reply_to_message:
        try:
            get_target_user_id_from_message(update.message.reply_to_message)
        except ValueError:
            await handle_expecting_input(update, context)
            return

    if not update.message.reply_to_message:
        await handle_expecting_input(update, context)
        return
    
    original_msg = update.message.reply_to_message
    try:
        target_id = get_target_user_id_from_message(original_msg)
    except ValueError:
        await update.message.reply_text(
            "❌ لم أجد أيدي المستخدم في الرسالة التي رددت عليها. "
            "ردّ على إشعار رسالة المستخدم الذي أرسله البوت."
        )
        return

    try:
        if update.message.text is not None:
            await context.bot.send_message(
                chat_id=target_id,
                text=update.message.text,
            )
        else:
            await context.bot.forward_message(
                chat_id=target_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
    except Exception:
        logger.exception("تعذّر إرسال رد المالك إلى المستخدم %s", target_id)
        await update.message.reply_text(
            "❌ تعذّر إرسال الرد للمستخدم. قد يكون قد حظر البوت أو أوقف المحادثة."
        )
        return

    await update.message.reply_text("✅ تم إرسال الرد للمستخدم.")

# --- أوامر الحظر والتقييد ---

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("قم بالرد على رسالة المستخدم لتنفيذ الأمر.")
        return
    
    try:
        target_id = get_target_user_id_from_message(update.message.reply_to_message)
        
        set_user_status(target_id, "ban", 1)
        await update.message.reply_text(f"✅ تم حظر المستخدم `{target_id}`.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("قم بالرد على رسالة المستخدم.")
        return
    
    try:
        target_id = get_target_user_id_from_message(update.message.reply_to_message)
        
        set_user_status(target_id, "ban", 0)
        await update.message.reply_text(f"✅ تم رفع الحظر عن `{target_id}`.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

async def restrict_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("قم بالرد على رسالة المستخدم.")
        return
    
    try:
        target_id = get_target_user_id_from_message(update.message.reply_to_message)
        
        set_user_status(target_id, "restrict", 1)
        await update.message.reply_text(f"✅ تم تقييد المستخدم `{target_id}`.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

async def unrestrict_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("قم بالرد على رسالة المستخدم.")
        return
    
    try:
        target_id = get_target_user_id_from_message(update.message.reply_to_message)
        
        set_user_status(target_id, "restrict", 0)
        await update.message.reply_text(f"✅ تم رفع التقييد عن `{target_id}`.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

async def user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("قم بالرد على رسالة المستخدم.")
        return
    
    try:
        target_id = get_target_user_id_from_message(update.message.reply_to_message)
        
        user = get_user(target_id)
        if user:
            await update.message.reply_text(f"📊 عدد رسائل المستخدم `{target_id}`: {user[3]}")
        else:
            await update.message.reply_text("المستخدم غير موجود في قاعدة البيانات.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}")

# --- إعدادات النصوص عبر المحادثة ---

async def edit_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    await query.edit_message_text(
        "أرسل نص الترحيب مع التنسيق والإيموجي المخصص الذي تريده، "
        "أو أرسل ملصقًا لإضافته إلى رسالة الترحيب."
    )
    context.user_data['expecting'] = 'welcome'

async def edit_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    await query.edit_message_text(
        "أرسل نص التأكيد مع التنسيق والإيموجي المخصص الذي تريده، "
        "أو أرسل ملصقًا لإضافته إلى رسالة التأكيد."
    )
    context.user_data['expecting'] = 'ack'

async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    await query.edit_message_text("أرسل معرف القناة (مثال: @channelusername) أو أيديها:")
    context.user_data['expecting'] = 'channel'

async def set_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    await query.edit_message_text("أرسل معرف البوت الإجباري (مثال: @botusername):")
    context.user_data['expecting'] = 'bot'

async def handle_expecting_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة النصوص المدخلة من المالك لتغيير الإعدادات"""
    if update.effective_user.id != config.OWNER_ID:
        return
    expecting = context.user_data.get('expecting')
    if not expecting:
        return

    if expecting == "button_icon":
        target = context.user_data.get("button_icon_target")
        if target not in BUTTON_ICON_LABELS:
            context.user_data.pop("expecting", None)
            context.user_data.pop("button_icon_target", None)
            await update.message.reply_text(
                "❌ انتهت جلسة تخصيص الأيقونة. افتح إعدادات الأزرار وحاول مجددًا."
            )
            return

        if (update.message.text or "").strip() in {"حذف", "إزالة", "مسح"}:
            set_setting(f"button_icon_{target}", "")
            context.user_data.pop("expecting", None)
            context.user_data.pop("button_icon_target", None)
            await update.message.reply_text(
                f"✅ تمت إزالة أيقونة زر «{BUTTON_ICON_LABELS[target]}»."
            )
            return

        custom_emoji = next(
            (
                entity
                for entity in (update.message.entities or [])
                if entity.type == MessageEntity.CUSTOM_EMOJI
                and entity.custom_emoji_id
            ),
            None,
        )
        if custom_emoji:
            set_setting(
                f"button_icon_{target}",
                custom_emoji.custom_emoji_id,
            )
            context.user_data.pop("expecting", None)
            context.user_data.pop("button_icon_target", None)
            await update.message.reply_text(
                f"✅ تم حفظ أيقونة Premium لزر «{BUTTON_ICON_LABELS[target]}»."
            )
            return

        await update.message.reply_text(
            "لم أجد إيموجي Premium مخصصًا في رسالتك. أرسل إيموجي Premium واحدًا "
            "أو أرسل «حذف» لإزالة الأيقونة."
        )
        return

    if expecting in ('welcome', 'ack') and update.message.sticker:
        set_setting(
            f"{expecting}_sticker_chat_id",
            str(update.effective_chat.id),
        )
        set_setting(
            f"{expecting}_sticker_message_id",
            str(update.message.message_id),
        )
        label = "الترحيب" if expecting == "welcome" else "التأكيد"
        await update.message.reply_text(
            f"✅ تمت إضافة الملصق إلى رسالة {label}، وسيعيد البوت توجيه الملصق الأصلي."
        )
        del context.user_data['expecting']
        return
    
    text = update.message.text
    if text is None:
        await update.message.reply_text(
            "أرسل نصًا، أو أرسل ملصقًا عند تعديل رسالة الترحيب أو التأكيد."
        )
        return
    if expecting == 'welcome':
        set_setting("welcome_message", text)
        set_setting(
            "welcome_entities",
            json.dumps(
                [entity.to_dict() for entity in (update.message.entities or [])],
                ensure_ascii=False,
            ),
        )
        set_setting("welcome_sticker_chat_id", "")
        set_setting("welcome_sticker_message_id", "")
        await update.message.reply_text(
            "✅ تم تحديث رسالة الترحيب مع تنسيقها وإيموجيها المخصص."
        )
    elif expecting == 'ack':
        set_setting("ack_message", text)
        set_setting(
            "ack_entities",
            json.dumps(
                [entity.to_dict() for entity in (update.message.entities or [])],
                ensure_ascii=False,
            ),
        )
        set_setting("ack_sticker_chat_id", "")
        set_setting("ack_sticker_message_id", "")
        await update.message.reply_text(
            "✅ تم تحديث رسالة التأكيد مع تنسيقها وإيموجيها المخصص."
        )
    elif expecting == 'channel':
        set_setting("force_channel", text)
        await update.message.reply_text(f"✅ تم تعيين القناة الإجبارية: {text}")
    elif expecting == 'bot':
        set_setting("force_bot", text)
        await update.message.reply_text(f"✅ تم تعيين البوت الإجباري: {text}")
    elif expecting == 'broadcast':
        set_setting("broadcast_text", text)
        await update.message.reply_text(
            "✅ تم حفظ الرسالة العامة. لإرسالها إلى المستخدمين، أرسل /broadcast."
        )
    
    del context.user_data['expecting']

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    users = get_all_users()
    msg = get_setting("broadcast_text")
    if not msg:
        await update.message.reply_text("لا توجد رسالة عامة محفوظة. استخدم /set_broadcast لتعيينها.")
        return
    
    success = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            success += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ تم إرسال الرسالة إلى {success} مستخدم.")

async def set_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    context.user_data['expecting'] = 'broadcast'
    await update.message.reply_text("أرسل نص الرسالة العامة:")

# --- تشغيل البوت ---

def main():
    init_db()
    application = Application.builder().token(config.BOT_TOKEN).build()
    
    # أوامر المستخدم
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", owner_panel))
    application.add_handler(MessageHandler(
        filters.TEXT
        & filters.User(config.OWNER_ID)
        & filters.Regex(r"(?i)^\s*(?:/admin(?:@\w+)?|ادمن|أدمن)\s*$"),
        owner_panel,
    ))
    application.add_handler(MessageHandler(
        filters.ALL
        & ~filters.COMMAND
        & filters.User(config.OWNER_ID),
        handle_owner_reply,
    ))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_user_message))
    
    # أوامر المالك
    application.add_handler(CommandHandler("owner", owner_panel))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("unban", unban_user))
    application.add_handler(CommandHandler("restrict", restrict_user))
    application.add_handler(CommandHandler("unrestrict", unrestrict_user))
    application.add_handler(CommandHandler("stats", user_stats))
    application.add_handler(CommandHandler("set_broadcast", set_broadcast))
    application.add_handler(CommandHandler("broadcast", broadcast))
    
    # أزرار لوحة التحكم
    application.add_handler(CallbackQueryHandler(owner_panel, pattern="^(?:open_admin_panel|back_owner)$"))
    application.add_handler(CallbackQueryHandler(set_gender, pattern="^set_gender:"))
    application.add_handler(CallbackQueryHandler(owner_stats_panel, pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(broadcast_setup, pattern="^broadcast_setup$"))
    application.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings$"))
    application.add_handler(CallbackQueryHandler(button_icons_menu, pattern="^button_icons$"))
    application.add_handler(CallbackQueryHandler(set_button_icon, pattern="^set_button_icon:"))
    application.add_handler(CallbackQueryHandler(moderation_help, pattern="^moderation_help$"))
    application.add_handler(CallbackQueryHandler(edit_welcome, pattern="^edit_welcome$"))
    application.add_handler(CallbackQueryHandler(edit_ack, pattern="^edit_ack$"))
    application.add_handler(CallbackQueryHandler(set_channel, pattern="^set_channel$"))
    application.add_handler(CallbackQueryHandler(set_bot, pattern="^set_bot$"))
    
    print("البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
