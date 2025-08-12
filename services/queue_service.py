# services/queue_service.py
import time
import logging
import hashlib
import json
from datetime import datetime
import httpx
import threading
from database.db import get_table
from config import ADMIN_MAIN_ID
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

# محاولة استخدام طبقة idempotency لو متاحة
try:
    from services import idempotency as _idem
except Exception:
    _idem = None

QUEUE_TABLE = "pending_requests"
_queue_lock = threading.Lock()
_queue_cooldown = False  # يمنع إظهار أكثر من طلب

# حد أقصى آمن لكابتشن الصور في تليجرام (نخلّيه أقل من 1024 بهامش)
_MAX_CAPTION = 900

def _idem_acquire(key: str, ttl_seconds: int) -> bool:
    # fallback محلي للمفاتيح
    if _idem is None:
        _CACHE = getattr(_idem_acquire, "_CACHE", {})
        now = time.time()
        for k, exp in list(_CACHE.items()):
            if exp <= now:
                _CACHE.pop(k, None)
        if key in _CACHE and _CACHE[key] > now:
            return False
        _CACHE[key] = now + ttl_seconds
        _idem_acquire._CACHE = _CACHE
        return True
    try:
        if hasattr(_idem, "acquire"):
            return bool(_idem.acquire(key, ttl_seconds))
        if hasattr(_idem, "begin"):
            return bool(_idem.begin(key, ttl_seconds))
    except Exception:
        return True
    return True

def _idem_release(key: str):
    if _idem is None:
        _CACHE = getattr(_idem_acquire, "_CACHE", {})
        _CACHE.pop(key, None)
        _idem_acquire._CACHE = _CACHE
        return
    try:
        if hasattr(_idem, "release"):
            _idem.release(key)
    except Exception:
        pass

def _compute_pending_key(user_id: int, request_text: str, payload):
    """
    مفتاح idempotency لصف الطلبات المعلّقة:
      - لو فيه hold_id داخل payload → نستعمله (الأفضل).
      - وإلا نعمل هاش من user_id + request_text + payload.
    """
    if isinstance(payload, dict):
        hid = payload.get("hold_id") or payload.get("idempotency_key")
        if hid:
            return f"pending:{user_id}:{hid}"
    try:
        s = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        s = str(payload)
    base = f"pending:{user_id}:{(request_text or '').strip()}:{s}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def add_pending_request(user_id: int, username: str, request_text: str, payload=None):
    """
    يضيف طلب للطابور مع حماية idempotency (TTL = 120 ثانية):
    نفس الطلب مش هيتكرر للإدمن لو الزبون ضغط تأكيد مرتين بسرعة.
    """
    idem_key = _compute_pending_key(user_id, request_text, payload)
    if not _idem_acquire(idem_key, ttl_seconds=120):
        logging.info(f"[QUEUE] duplicate suppressed for key={idem_key}")
        return

    try:
        data = {
            "user_id": user_id,
            "username": username,
            "request_text": request_text,
            "created_at": datetime.utcnow().isoformat()
        }
        if payload is not None:
            # خزّن المفتاح للمرجعة (اختياري)
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.setdefault("idempotency_key", idem_key)
            data["payload"] = payload

        get_table(QUEUE_TABLE).insert(data).execute()
        return
    except httpx.ReadError as e:
        logging.warning(f"ReadError in add_pending_request: {e}")
    except Exception:
        logging.exception("Unexpected error in add_pending_request")
    finally:
        # لو فشلنا في الإدراج لأي سبب، حرّر المفتاح عشان يقدر يحاول تاني
        # (لو نجح الإدراج بنعتمد على TTL للمفتاح)
        try:
            _idem_release(idem_key)
        except Exception:
            pass

def delete_pending_request(request_id: int):
    try:
        get_table(QUEUE_TABLE).delete().eq("id", request_id).execute()
    except Exception:
        logging.exception(f"Error deleting pending request {request_id}")

def get_next_request():
    try:
        res = (
            get_table(QUEUE_TABLE)
            .select("*")
            .order("created_at")
            .limit(1)
            .execute()
        )
        data = res.data or []
        return data[0] if data else None
    except httpx.ReadError as e:
        logging.warning(f"ReadError in get_next_request: {e}")
        return None
    except Exception:
        logging.exception("Unexpected error in get_next_request")
        return None

def update_request_admin_message_id(request_id: int, message_id: int):
    logging.debug(f"Skipping update_request_admin_message_id for request {request_id}")

def postpone_request(request_id: int):
    try:
        now = datetime.utcnow().isoformat()
        get_table(QUEUE_TABLE) \
            .update({"created_at": now}) \
            .eq("id", request_id) \
            .execute()
    except Exception:
        logging.exception(f"Error postponing request {request_id}")

def _send_admin_with_photo(bot, photo_id: str, text: str, keyboard: InlineKeyboardMarkup):
    """
    يرسل صورة + رسالة الإدمن.
    لو النص أطول من حد الكابتشن، نرسل كابتشن قصير ثم رسالة كاملة مع الأزرار.
    """
    try:
        if text and len(text) <= _MAX_CAPTION:
            bot.send_photo(
                ADMIN_MAIN_ID,
                photo_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            # كابتشن قصير + نص كامل بعده
            bot.send_photo(
                ADMIN_MAIN_ID,
                photo_id,
                caption="🖼️ تفاصيل الطلب في الرسالة التالية ⬇️",
                parse_mode="HTML"
            )
            bot.send_message(
                ADMIN_MAIN_ID,
                text or "طلب جديد",
                parse_mode="HTML",
                reply_markup=keyboard
            )
    except Exception:
        logging.exception("Failed sending admin photo/message; falling back to text-only")
        bot.send_message(
            ADMIN_MAIN_ID,
            text or "طلب جديد",
            parse_mode="HTML",
            reply_markup=keyboard
        )

def process_queue(bot):
    global _queue_cooldown
    if _queue_cooldown:
        return

    with _queue_lock:
        req = get_next_request()
        if not req:
            return

        request_id = req.get("id")
        text = req.get("request_text", "") or "طلب جديد"
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("🔁 تأجيل", callback_data=f"admin_queue_postpone_{request_id}"),
            InlineKeyboardButton("✅ تأكيد",  callback_data=f"admin_queue_accept_{request_id}"),
            InlineKeyboardButton("🚫 إلغاء", callback_data=f"admin_queue_cancel_{request_id}"),
            InlineKeyboardButton("✉️ رسالة للعميل", callback_data=f"admin_queue_message_{request_id}"),
            InlineKeyboardButton("🖼️ صورة للعميل", callback_data=f"admin_queue_photo_{request_id}")
        )

        payload  = req.get("payload") or {}
        typ      = payload.get("type")
        photo_id = payload.get("photo")

        # =========== فرع شحن المحفظة ===========
        if typ == "recharge" and photo_id:
            _send_admin_with_photo(bot, photo_id, text, keyboard)

        # =========== فرع إعلانات القناة ===========
        elif typ == "ads":
            images = payload.get("images", [])
            if images:
                if len(images) == 1:
                    _send_admin_with_photo(bot, images[0], text, keyboard)
                else:
                    # مجموعة صور أولًا (بدون أزرار)، ثم رسالة التفاصيل مع الأزرار
                    try:
                        media = [InputMediaPhoto(fid) for fid in images]
                        bot.send_media_group(ADMIN_MAIN_ID, media)
                    except Exception:
                        logging.exception("Failed to send media group, fallback to message only")
                    bot.send_message(
                        ADMIN_MAIN_ID,
                        text,
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
            else:
                bot.send_message(
                    ADMIN_MAIN_ID,
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        # =========== الأنواع الأخرى ===========
        else:
            bot.send_message(
                ADMIN_MAIN_ID,
                text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

def queue_cooldown_start(bot=None):
    global _queue_cooldown
    _queue_cooldown = True
    def release():
        global _queue_cooldown
        time.sleep(60)
        _queue_cooldown = False
        if bot is not None:
            process_queue(bot)
    threading.Thread(target=release, daemon=True).start()
