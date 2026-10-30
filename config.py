import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
DB_NAME = "site_bot.db"
BUSINESS_MEDIA_DIR = os.getenv("BUSINESS_MEDIA_DIR", "business_media")
