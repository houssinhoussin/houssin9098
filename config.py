# config.py
import os
import logging
from dotenv import load_dotenv

# حمّل متغيرات البيئة من ملف .env (إن وجد)
load_dotenv()

# لوج افتراضي معقول
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

def _get(name, default=None, cast=None, required=False):
    val = os.getenv(name)
    if (val is None or val == "") and required and default is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    if val is None or val == "":
        val = default
    if cast and val is not None:
        try:
            val = cast(val)
        except Exception:
            raise RuntimeError(f"Invalid value for {name}: expected {cast.__name__}")
    return val

# --- Telegram ---
API_TOKEN    = _get("API_TOKEN", required=True)
BOT_USERNAME = _get("BOT_USERNAME", required=True)
if BOT_USERNAME.startswith("@"):
    BOT_USERNAME = BOT_USERNAME[1:]
BOT_NAME     = _get("BOT_NAME", "Bot")
BOT_ID       = _get("BOT_ID", cast=int, required=True)

# --- Admin ---
ADMIN_MAIN_ID       = _get("ADMIN_MAIN_ID", cast=int, required=True)
ADMIN_MAIN_USERNAME = _get("ADMIN_MAIN_USERNAME")

def _parse_admin_ids(s: str | None, fallback: int | None):
    ids = []
    if s:
        for part in s.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                ids.append(int(p))
            except ValueError:
                raise RuntimeError(f"Invalid admin id in ADMINS: {p}")
    if not ids and fallback is not None:
        ids = [int(fallback)]
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            ordered.append(i); seen.add(i)
    return ordered

# من .env (اختياري): ADMINS=6935846121,123456...
ADMINS = _parse_admin_ids(os.getenv("ADMINS"), ADMIN_MAIN_ID)

# --- Force Sub / Channel ---
FORCE_SUB_CHANNEL_ID       = _get("FORCE_SUB_CHANNEL_ID")
FORCE_SUB_CHANNEL_USERNAME = _get("FORCE_SUB_CHANNEL_USERNAME")
CHANNEL_USERNAME           = _get("CHANNEL_USERNAME")

# --- Webhook / Polling ---
WEBHOOK_URL = _get("WEBHOOK_URL")          # إن وجد نعتبر التشغيل Webhook
IS_WEBHOOK = bool(WEBHOOK_URL)
LONG_POLLING_TIMEOUT = _get("LONG_POLLING_TIMEOUT", 25, cast=int)

# --- General ---
LANG        = _get("LANG", "ar")
ENCODING    = _get("ENCODING", "utf-8")
PAYEER_RATE = _get("PAYEER_RATE", 0, cast=int)
TELEGRAM_PARSE_MODE = _get("TELEGRAM_PARSE_MODE", "HTML")

# --- Supabase ---
SUPABASE_URL        = _get("SUPABASE_URL", required=True)
SUPABASE_KEY        = _get("SUPABASE_KEY") or _get("SUPABASE_API_KEY")
SUPABASE_TABLE_NAME = _get("SUPABASE_TABLE_NAME", "houssin363")  # جدول المستخدمين/الرصيد
if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY (or SUPABASE_API_KEY) in environment")

# ============================================================
# أسماء الجداول (مطابقة تمامًا لما عندك في Supabase)
# يمكن تغييرها عبر .env إذا لزم بدون المساس بالكود
# ============================================================
TABLE_USERS                    = _get("TABLE_USERS", "houssin363")
TABLE_USER_STATE               = _get("TABLE_USER_STATE", "user_state")
TABLE_PENDING_REQUEST          = _get("TABLE_PENDING_REQUEST", "pending_requests")
TABLE_CHANNEL_ADS              = _get("TABLE_CHANNEL_ADS", "channel_ads")

TABLE_PRODUCTS                 = _get("TABLE_PRODUCTS", "Products")
TABLE_PURCHASES                = _get("TABLE_PURCHASES", "purchases")
TABLE_TRANSACTIONS             = _get("TABLE_TRANSACTIONS", "transactions")

TABLE_ADS_PURCHASES            = _get("TABLE_ADS_PURCHASES", "ads_purchases")
TABLE_BILL_UNITS_PURCHASES     = _get("TABLE_BILL_UNITS_PURCHASES", "bill_and_units_purchases")
TABLE_CASH_TRANSFER_PURCHASES  = _get("TABLE_CASH_TRANSFER_PURCHASES", "cash_transfer_purchases")
TABLE_COMPANIES_TRANSFER_PURCH = _get("TABLE_COMPANIES_TRANSFER_PURCH", "companies_transfer_purchases")
TABLE_INTERNET_PROV_PURCHASES  = _get("TABLE_INTERNET_PROV_PURCHASES", "internet_providers_purchases")
TABLE_UNIVERSITY_FEES_PURCHASES= _get("TABLE_UNIVERSITY_FEES_PURCHASES", "university_fees_purchases")
TABLE_WHOLESALE_PURCHASES      = _get("TABLE_WHOLESALE_PURCHASES", "wholesale_purchases")
