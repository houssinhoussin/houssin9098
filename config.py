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

# === الدوال التي طلبت إضافتها ===
def _as_int(x, default=None):
    try:
        return int(str(x).strip())
    except Exception:
        return default

def _as_int_list(csv, default=None):
    if not csv:
        return default or []
    out = []
    for part in str(csv).split(","):
        part = part.strip()
        if part:
            v = _as_int(part)
            if v is not None:
                out.append(v)
    return out

# --- Telegram ---
# نحافظ على نفس الاستدعاء لكن نوفر القيم الحقيقية كقيم افتراضية
API_TOKEN    = _get("API_TOKEN", "7936418161:AAGNZEMIGZEmPfYlCGQbO_vM9oQbQUVSiT4")
BOT_USERNAME = _get("BOT_USERNAME", "@my_fast_shop_bot")
BOT_NAME     = _get("BOT_NAME", "المتجر العالمي")
BOT_ID       = _get("BOT_ID", 7936418161, cast=int)

# --- Admin ---
ADMIN_MAIN_ID       = _get("ADMIN_MAIN_ID", 6935846121, cast=int)
ADMIN_MAIN_USERNAME = _get("ADMIN_MAIN_USERNAME", "@Houssin363")

def _parse_admin_ids(s: str | None, fallback: int):
    # نُبقي الدالة كما هي منطقيًا، لكن نستخدم الدوال الجديدة داخليًا
    default_list = [fallback] if fallback is not None else []
    return _as_int_list(s or "", default=default_list)

# من .env: ADMINS=6935846121,123456...
# إن لم يوجد المتغير ستُستخدم القيم التي زودتها
ADMINS = _parse_admin_ids(os.getenv("ADMINS") or "6935846121,5401037337", ADMIN_MAIN_ID)

# --- Force Sub / Channel ---
# نحتفظ بها كنص لأن قنوات تيليغرام IDs تكون بصيغة -100xxxx وتعمل كنص
FORCE_SUB_CHANNEL_ID       = _get("FORCE_SUB_CHANNEL_ID", "-1002852510917")
FORCE_SUB_CHANNEL_USERNAME = _get("FORCE_SUB_CHANNEL_USERNAME", "@shop100sho")
CHANNEL_USERNAME           = _get("CHANNEL_USERNAME", "@shop100sho")

# --- Webhook (اختياري) ---
WEBHOOK_URL = _get("WEBHOOK_URL", "https://telegram-shop-bot-lo4t.onrender.com/")

# --- General ---
LANG        = _get("LANG", "ar")
ENCODING    = _get("ENCODING", "utf-8")
PAYEER_RATE = _get("PAYEER_RATE", 9000, cast=int)

# --- Supabase ---
SUPABASE_URL        = _get("SUPABASE_URL", "https://azortroeejjomqweintc.supabase.co")
SUPABASE_TABLE_NAME = _get("SUPABASE_TABLE_NAME", "USERS_TABLE")

# نُبقي نفس منطق التفضيل مع توفير مفاتيحك كقيمة افتراضية
_supabase_key = _get("SUPABASE_KEY")
if not _supabase_key:
    _supabase_key = _get("SUPABASE_API_KEY")
if not _supabase_key:
    _supabase_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF6b3J0cm9lZWpqb21xd2VpbnRjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTIxOTIzNjUsImV4cCI6MjA2Nzc2ODM2NX0.x3Pwq8OyRmlr7JQuEU2xRxYJtSoz67eIVzDx8Nh4muk"

SUPABASE_KEY = _supabase_key

# --- طباعة دور المفتاح (service_role أو anon) للمساعدة في التشخيص ---
def _jwt_role(jwt: str | None):
    if not jwt:
        return None
    try:
        import base64, json
        parts = jwt.split('.')
        if len(parts) < 2:
            return None
        pad = '=' * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode())
        return payload.get("role") or payload.get("user_role")
    except Exception:
        return None

_role = _jwt_role(SUPABASE_KEY)
logging.info(f"Supabase auth role: {_role or 'unknown'}")
if _role != 'service_role':
    logging.warning("⚠️ SUPABASE_KEY ليس service_role (غالبًا anon). قد تمنع RLS عمليات الكتابة. ضع service_role في متغيرات البيئة على الخادم.")

if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY (or SUPABASE_API_KEY) in environment")


@bot.message_handler(commands=['cancel'])
def cancel_cmd(m):
    try:
        for dct in (globals().get('_msg_by_id_pending', {}),
                    globals().get('_disc_new_user_state', {}),
                    globals().get('_admin_manage_user_state', {}),
                    globals().get('_address_state', {}),
                    globals().get('_phone_state', {})):
            try:
                dct.pop(m.from_user.id, None)
            except Exception:
                pass
    except Exception:
        pass
    try:
        bot.reply_to(m, "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.")
    except Exception:
        bot.send_message(m.chat.id, "✅ تم الإلغاء.")
