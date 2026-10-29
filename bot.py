import logging
import re
from html import escape as html_escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode
import config
from database import (
    init_db, get_setting, set_setting, add_user, increment_message_count,
    get_user, set_user_status, get_all_users
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def extract_target_user_id(message_text: str) -> int:
    """استخراج معرّف المستخدم بعد إزالة Telegram لوسوم التنسيق."""
    match = re.search(
        r"(?m)^\s*🆔\s*(?:الأيدي|ID)\s*:\s*`?(\d+)`?\s*$",
        message_text or "",
    )
    if not match:
        raise ValueError("لم يتم العثور على أيدي المستخدم في الرسالة.")
    return int(match.group(1))


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.OWNER_ID


async def reject_non_owner_callback(update: Update) -> bool:
    if is_owner(update):
        return False
    await update.callback_query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
    return True


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
    
    welcome = get_setting("welcome_message")
    reply_markup = None
    if is_owner(update):
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("👑 لوحة الإدارة", callback_data="open_admin_panel")
        ]])
    await update.message.reply_text(welcome, reply_markup=reply_markup)

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
    owner_msg += f"💬 النص:\n{html_escape(update.message.text)}"

    await context.bot.send_message(
        chat_id=config.OWNER_ID,
        text=owner_msg,
        parse_mode=ParseMode.HTML,
    )
    
    # الرد على المستخدم برسالة التأكيد
    ack = get_setting("ack_message")
    await update.message.reply_text(ack)

# --- أوامر المالك ---

async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(update):
        if query:
            await query.answer("هذه اللوحة للمالك فقط.", show_alert=True)
        return
    
    keyboard = [
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")],
        [InlineKeyboardButton("🛡 إدارة المستخدمين", callback_data="moderation_help")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="stats")],
        [InlineKeyboardButton("📢 إعداد رسالة عامة", callback_data="broadcast_setup")]
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
        [InlineKeyboardButton("✏️ تغيير رسالة الترحيب", callback_data="edit_welcome")],
        [InlineKeyboardButton("✏️ تغيير رسالة التأكيد", callback_data="edit_ack")],
        [InlineKeyboardButton("📢 تعيين القناة الإجبارية", callback_data="set_channel")],
        [InlineKeyboardButton("🤖 تعيين البوت الإجباري", callback_data="set_bot")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_owner")]
    ]
    await query.edit_message_text("الإعدادات:", reply_markup=InlineKeyboardMarkup(keyboard))


async def moderation_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_owner")]]
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
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_owner")]]
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
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_owner")]]
    await query.edit_message_text(
        "أرسل نص الرسالة العامة لحفظها. بعد الحفظ، أرسل /broadcast لإرسالها للمستخدمين.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عندما يرد المالك على رسالة، تصل للمستخدم الأصلي"""
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
        await handle_expecting_input(update, context)
        return
    
    original_msg = update.message.reply_to_message
    try:
        target_id = extract_target_user_id(
            original_msg.text or original_msg.caption or ""
        )
    except ValueError:
        await update.message.reply_text(
            "❌ لم أجد أيدي المستخدم في الرسالة التي رددت عليها. "
            "ردّ على إشعار رسالة المستخدم الذي أرسله البوت."
        )
        return

    try:
        await context.bot.send_message(chat_id=target_id, text=update.message.text)
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
        target_id = extract_target_user_id(
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        
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
        target_id = extract_target_user_id(
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        
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
        target_id = extract_target_user_id(
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        
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
        target_id = extract_target_user_id(
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        
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
        target_id = extract_target_user_id(
            update.message.reply_to_message.text
            or update.message.reply_to_message.caption
            or ""
        )
        
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
    await query.edit_message_text("أرسل رسالة الترحيب الجديدة:")
    context.user_data['expecting'] = 'welcome'

async def edit_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await reject_non_owner_callback(update):
        return
    await query.answer()
    await query.edit_message_text("أرسل رسالة التأكيد الجديدة:")
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
    
    text = update.message.text
    if expecting == 'welcome':
        set_setting("welcome_message", text)
        await update.message.reply_text("✅ تم تحديث رسالة الترحيب.")
    elif expecting == 'ack':
        set_setting("ack_message", text)
        await update.message.reply_text("✅ تم تحديث رسالة التأكيد.")
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(config.OWNER_ID), handle_owner_reply))
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
    application.add_handler(CallbackQueryHandler(owner_stats_panel, pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(broadcast_setup, pattern="^broadcast_setup$"))
    application.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings$"))
    application.add_handler(CallbackQueryHandler(moderation_help, pattern="^moderation_help$"))
    application.add_handler(CallbackQueryHandler(edit_welcome, pattern="^edit_welcome$"))
    application.add_handler(CallbackQueryHandler(edit_ack, pattern="^edit_ack$"))
    application.add_handler(CallbackQueryHandler(set_channel, pattern="^set_channel$"))
    application.add_handler(CallbackQueryHandler(set_bot, pattern="^set_bot$"))
    
    print("البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
