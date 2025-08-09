# services/queue_service.py
import time
import logging
from datetime import datetime
import threading
import httpx

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

from database.db import get_table
from config import ADMIN_MAIN_ID, TABLE_PENDING_REQUEST
# لو ما كان موجود في config لأي سبب، نرجع للقيمة القديمة
QUEUE_TABLE = TABLE_PENDING_REQUEST or "pending_requests"

# قفل لحماية الوصول للطابور
_queue_lock = threading.Lock()
# كولداون اختياري لمنع وابل رسائل للأدمن
_queue_cooldown = False

def add_pending_request(user_id: int, username: str, request_text: str, payload=None):
    """
    يضيف طلبًا جديدًا إلى جدول الطابور. يبقى التوقيع كما هو.
    """
    for attempt in range(1, 4):
        try:
            data = {
                "user_id": user_id,
                "username": username,
                "request_text": request_text,
                "created_at": datetime.utcnow().isoformat()
            }
            if payload is not None:
                data["payload"] = payload
            get_table(QUEUE_TABLE).insert(data).execute()
            logging.info(f"[QUEUE] أُضيف طلب جديد للمستخدم {user_id}")
            return
        except httpx.ReadError as e:
            logging.warning(f"[QUEUE] Attempt {attempt}: ReadError in add_pending_request: {e}")
            time.sleep(0.5)
        except Exception as e:
            logging.warning(f"[QUEUE] Attempt {attempt}: error in add_pending_request: {e}")
            time.sleep(0.5)
    logging.error(f"[QUEUE] Failed to add pending request for user {user_id} after 3 attempts.")

def delete_pending_request(request_id: int):
    """
    يحذف طلبًا من الطابور حسب id.
    """
    try:
        get_table(QUEUE_TABLE).delete().eq("id", request_id).execute()
        logging.info(f"[QUEUE] حُذف الطلب {request_id}")
    except Exception:
        logging.exception(f"[QUEUE] Error deleting pending request {request_id}")

def get_next_request():
    """
    يجلب أقدم طلب (حسب created_at) لمعالجته.
    """
    try:
        res = (
            get_table(QUEUE_TABLE)
            .select("*")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        data = res.data or []
        req = data[0] if data else None
        if req:
            logging.debug(f"[QUEUE] next request id={req.get('id')}")
        return req
    except httpx.ReadError as e:
        logging.warning(f"[QUEUE] ReadError in get_next_request: {e}")
        return None
    except Exception:
        logging.exception("[QUEUE] Unexpected error in get_next_request")
        return None

def update_request_admin_message_id(request_id: int, message_id: int):
    """
    (اختياري) لو أردت أن تحفظ message_id لرسالة الأدمن.
    أبقيناها كـ no-op كما كانت.
    """
    logging.debug(f"[QUEUE] Skipping update_request_admin_message_id for request {request_id}")

def postpone_request(request_id: int):
    """
    يؤخّر الطلب بإعادة created_at إلى الآن (ينقله لآخر الطابور).
    """
    try:
        now = datetime.utcnow().isoformat()
        (
            get_table(QUEUE_TABLE)
            .update({"created_at": now})
            .eq("id", request_id)
            .execute()
        )
        logging.info(f"[QUEUE] تم تأجيل الطلب {request_id}")
    except Exception:
        logging.exception(f"[QUEUE] Error postponing request {request_id}")

def process_queue(bot):
    """
    يسحب طلبًا واحدًا (إن وُجد) ويرسله للأدمن مع أزرار الإدارة.
    تُستدعى دوريًا (مثلاً من ثريد في main.py) أو بعد add_pending_request.
    """
    global _queue_cooldown
    if _queue_cooldown:
        return

    with _queue_lock:
        req = get_next_request()
        if not req:
            return

        request_id = req.get("id")
        text = req.get("request_text", "") or ""
        payload  = req.get("payload") or {}
        typ      = payload.get("type")
        photo_id = payload.get("photo")

        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("🔁 تأجيل",  callback_data=f"admin_queue_postpone_{request_id}"),
            InlineKeyboardButton("✅ تأكيد",   callback_data=f"admin_queue_accept_{request_id}"),
            InlineKeyboardButton("🚫 إلغاء",  callback_data=f"admin_queue_cancel_{request_id}"),
            InlineKeyboardButton("✉️ رسالة للعميل", callback_data=f"admin_queue_message_{request_id}"),
            InlineKeyboardButton("🖼️ صورة للعميل",  callback_data=f"admin_queue_photo_{request_id}"),
        )

        try:
            # =========== فرع شحن المحفظة (مع صورة إثبات) ===========
            if typ == "recharge" and photo_id:
                bot.send_photo(
                    ADMIN_MAIN_ID,
                    photo_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            # =========== فرع إعلانات القناة ===========
            elif typ == "ads":
                images = payload.get("images", []) or []
                if images:
                    if len(images) == 1:
                        bot.send_photo(
                            ADMIN_MAIN_ID,
                            images[0],
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                    else:
                        media = [InputMediaPhoto(fid) for fid in images]
                        bot.send_media_group(ADMIN_MAIN_ID, media)  # الصور أولاً
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
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        except Exception as e:
            logging.exception(f"[QUEUE] Failed to deliver request {request_id} to admin: {e}")

def queue_cooldown_start(bot=None):
    """
    يفعّل فترة تهدئة 60 ثانية، ثم يعاود استدعاء process_queue تلقائيًا.
    """
    global _queue_cooldown
    _queue_cooldown = True

    def release():
        global _queue_cooldown
        time.sleep(60)
        _queue_cooldown = False
        if bot is not None:
            try:
                process_queue(bot)
            except Exception:
                logging.exception("[QUEUE] Error while re-processing queue after cooldown")

    threading.Thread(target=release, daemon=True).start()

# نهاية ملف queue_service.py
