import time
import logging
from datetime import datetime
import httpx
import threading
from database.db import client
from config import ADMIN_MAIN_ID
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

QUEUE_TABLE = "pending_requests"
_queue_lock = threading.Lock()
_queue_cooldown = False  # يمنع إظهار أكثر من طلب

def add_pending_request(user_id: int, username: str, request_text: str, payload=None):
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
            client.table(QUEUE_TABLE).insert(data).execute()
            return
        except httpx.ReadError as e:
            logging.warning(f"Attempt {attempt}: ReadError in add_pending_request: {e}")
            time.sleep(0.5)
    logging.error(f"Failed to add pending request for user {user_id} after 3 attempts.")
    
def delete_pending_request(request_id: int):
    try:
        client.table(QUEUE_TABLE).delete().eq("id", request_id).execute()
    except Exception:
        logging.exception(f"Error deleting pending request {request_id}")

def get_next_request():
    try:
        res = (
            client.table(QUEUE_TABLE)
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
        client.table(QUEUE_TABLE) \
            .update({"created_at": now}) \
            .eq("id", request_id) \
            .execute()
    except Exception:
        logging.exception(f"Error postponing request {request_id}")

def process_queue(bot):
    global _queue_cooldown
    if _queue_cooldown:
        return

    with _queue_lock:
        req = get_next_request()
        if not req:
            return

        request_id = req.get("id")
        text = req.get("request_text", "")

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
            bot.send_photo(
                ADMIN_MAIN_ID,
                photo_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

        # =========== فرع إعلانات القناة ===========
        elif typ == "ads":
            images = payload.get("images", [])
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
                    bot.send_media_group(ADMIN_MAIN_ID, media)            # الصور أولاً
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
        time.sleep(120)
        _queue_cooldown = False
        if bot is not None:
            process_queue(bot)
    threading.Thread(target=release, daemon=True).start()

# نهاية ملف queue_service.py
