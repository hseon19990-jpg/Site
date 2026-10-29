import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler
)
import config
from database import (
    init_db, get_setting, set_setting, add_user, increment_message_count,
    get_user, set_user_status, get_all_users
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def extract_target_user_id(message_text: str) -> int:
    """استخراج معرّف المستخدم من إشعار المالك قبل أو بعد معالجة Markdown."""
    match = re.search(
        r"(?m)^\s*🆔\s*(?:الأيدي|ID)\s*:\s*`?(\d+)`?\s*$",
        message_text or "",
    )
    if not match:
        raise ValueError("لم يتم العثور على أيدي المستخدم في الرسالة.")
    return int(match.group(1))


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
    await update.message.reply_text(welcome)

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
    
    # أبقِ إشعار المالك نصاً عادياً حتى لا تكسر الأسماء أو الرسائل تنسيق Markdown.
    owner_msg = f"🆔 الأيدي: {user.id}\n"
    owner_msg += f"📩 رسالة جديدة من: {user.full_name}\n"
    if user.username:
        owner_msg += f"👤 اسم المستخدم: @{user.username}\n"
    owner_msg += f"📊 عدد الرسائل: {count}\n"
    owner_msg += f"💬 النص:\n{update.message.text}"

    await context.bot.send_message(chat_id=config.OWNER_ID, text=owner_msg)
    
    # الرد على المستخدم برسالة التأكيد
    ack = get_setting("ack_message")
    await update.message.reply_text(ack)

# --- أوامر المالك ---

async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.OWNER_ID:
        return
    
    keyboard = [
        [InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="stats")],
        [InlineKeyboardButton("📢 رسالة عامة", callback_data="broadcast")]
    ]
    await update.message.reply_text("لوحة تحكم المالك:", reply_markup=InlineKeyboardMarkup(keyboard))

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("✏️ تغيير رسالة الترحيب", callback_data="edit_welcome")],
        [InlineKeyboardButton("✏️ تغيير رسالة التأكيد", callback_data="edit_ack")],
        [InlineKeyboardButton("📢 تعيين القناة الإجبارية", callback_data="set_channel")],
        [InlineKeyboardButton("🤖 تعيين البوت الإجباري", callback_data="set_bot")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_owner")]
    ]
    await query.edit_message_text("الإعدادات:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عندما يرد المالك على رسالة، تصل للمستخدم الأصلي"""
    if update.effective_user.id != config.OWNER_ID:
        return
    if not update.message.reply_to_message:
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
        text = update.message.reply_to_message.text
        start = text.find("الأيدي: `") + len("الأيدي: `")
        end = text.find("`", start)
        target_id = int(text[start:end])
        
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
        text = update.message.reply_to_message.text
        start = text.find("الأيدي: `") + len("الأيدي: `")
        end = text.find("`", start)
        target_id = int(text[start:end])
        
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
        text = update.message.reply_to_message.text
        start = text.find("الأيدي: `") + len("الأيدي: `")
        end = text.find("`", start)
        target_id = int(text[start:end])
        
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
        text = update.message.reply_to_message.text
        start = text.find("الأيدي: `") + len("الأيدي: `")
        end = text.find("`", start)
        target_id = int(text[start:end])
        
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
        text = update.message.reply_to_message.text
        start = text.find("الأيدي: `") + len("الأيدي: `")
        end = text.find("`", start)
        target_id = int(text[start:end])
        
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
    await query.answer()
    await query.edit_message_text("أرسل رسالة الترحيب الجديدة:")
    context.user_data['expecting'] = 'welcome'

async def edit_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("أرسل رسالة التأكيد الجديدة:")
    context.user_data['expecting'] = 'ack'

async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("أرسل معرف القناة (مثال: @channelusername) أو أيديها:")
    context.user_data['expecting'] = 'channel'

async def set_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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
    application.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings$"))
    application.add_handler(CallbackQueryHandler(edit_welcome, pattern="^edit_welcome$"))
    application.add_handler(CallbackQueryHandler(edit_ack, pattern="^edit_ack$"))
    application.add_handler(CallbackQueryHandler(set_channel, pattern="^set_channel$"))
    application.add_handler(CallbackQueryHandler(set_bot, pattern="^set_bot$"))
    
    # معالجة إدخالات المالك (يجب أن تكون بعد الأوامر)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.User(config.OWNER_ID), handle_expecting_input))
    
    print("البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
