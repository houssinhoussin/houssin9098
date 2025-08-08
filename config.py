# config.py
import os
import logging
from dotenv import load_dotenv

# حمّل متغيرات البيئة من ملف .env (إن وجد)
load_dotenv()

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
BOT_NAME     = _get("BOT_NAME", "Bot")
BOT_ID       = _get("BOT_ID", cast=int, required=True)

# --- Admin ---
ADMIN_MAIN_ID       = _get("ADMIN_MAIN_ID", cast=int, required=True)
ADMIN_MAIN_USERNAME = _get("ADMIN_MAIN_USERNAME")

def _parse_admin_ids(s: str | None, fallback: int):
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
    return ids

# من .env: ADMINS=6935846121,123456...
ADMINS = _parse_admin_ids(os.getenv("ADMINS"), ADMIN_MAIN_ID)

# --- Force Sub / Channel ---
# نحتفظ بها كنص لأن قنوات تيليغرام IDs تكون بصيغة -100xxxx وتعمل كنص
FORCE_SUB_CHANNEL_ID       = _get("FORCE_SUB_CHANNEL_ID")
FORCE_SUB_CHANNEL_USERNAME = _get("FORCE_SUB_CHANNEL_USERNAME")
CHANNEL_USERNAME           = _get("CHANNEL_USERNAME")

# --- Webhook (اختياري) ---
WEBHOOK_URL = _get("WEBHOOK_URL")

# --- General ---
LANG        = _get("LANG", "ar")
ENCODING    = _get("ENCODING", "utf-8")
PAYEER_RATE = _get("PAYEER_RATE", 0, cast=int)

# --- Supabase ---
SUPABASE_URL        = _get("SUPABASE_URL", required=True)
SUPABASE_KEY        = _get("SUPABASE_KEY") or _get("SUPABASE_API_KEY")
SUPABASE_TABLE_NAME = _get("SUPABASE_TABLE_NAME", "houssin363")

if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY (or SUPABASE_API_KEY) in environment")
