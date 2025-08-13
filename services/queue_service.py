# -*- coding: utf-8 -*-
# services/queue_service.py

import time
import logging
from datetime import datetime
import httpx
import threading

from database.db import get_table
from config import ADMIN_MAIN_ID, ADMINS
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

QUEUE_TABLE = "pending_requests"

_queue_lock = threading.Lock()
_queue_cooldown = False

# منع التكرار ضمن نافذة قصيرة (حماية إضافية من تعدد الاستدعاءات)
_recently_sent = {}        # {request_id: last_ts}
_RECENT_TTL     = 40       # ثوانٍ

def _admin_targets():
    """إرجاع قائمة الإداريين (ADMINS + ADMIN_MAIN_ID) بدون تكرار، مع الحفاظ على الترتيب."""
    try:
        lst = list(ADMINS) if isinstance(ADMINS, (list, tuple, set)) else []
    except Exception:
        lst = []
    if ADMIN_MAIN_ID not in lst:
        lst.append(ADMIN_MAIN_ID)
    seen, out = set(), []
    for a in lst:
        if a not in seen:
            out.append(a)
            seen.add(a)
    return out

# حد أقصى آمن لكابتشن الصور في تليجرام (نخلّيه أقل من 1024 بهامش)
_MAX_CAPTION = 900

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
            get_table(QUEUE_TABLE).insert(data).execute()
            return
        except httpx.ReadError as e:
            logging.warning(f"Attempt {attempt}: ReadError in add_pending_request: {e}")
            time.sleep(0.5)
    logging.error(f"Failed to add pending request for user {user_id} after 3 attempts.")

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
            .order("created_at")  # تصاعديًا (متوافق مع نسختك من supabase-py)
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
    # يمكن لاحقًا حفظ message_id إن أردت التحكم في التكرار عبر DB
    logging.debug(f"Skipping update_request_admin_message_id for request {request_id}")

def postpone_request(request_id: int):
    """إرجاع الطلب لآخر الدور بتحديث created_at."""
    try:
        now = datetime.utcnow().isoformat()
        get_table(QUEUE_TABLE).update({"created_at": now}).eq("id", request_id).execute()
    except Exception:
        logging.exception(f"Error postponing request {request_id}")

def _send_admin_with_photo(bot, photo_id: str, text: str, keyboard: InlineKeyboardMarkup):
    try:
        if text and len(text) <= _MAX_CAPTION:
            for admin_id in _admin_targets():
                bot.send_photo(admin_id, photo_id, caption=text, parse_mode="HTML", reply_markup=keyboard)
        else:
            for admin_id in _admin_targets():
                bot.send_photo(admin_id, photo_id, caption="🖼️ تفاصيل الطلب في الرسالة التالية ⬇️", parse_mode="HTML")
                bot.send_message(admin_id, text or "طلب جديد", parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        logging.exception("Failed sending admin photo/message; falling back to text-only")
        for admin_id in _admin_targets():
            bot.send_message(admin_id, text or "طلب جديد", parse_mode="HTML", reply_markup=keyboard)

def process_queue(bot):
    global _queue_cooldown
    if _queue_cooldown:
        return

    with _queue_lock:
        req = get_next_request()
        if not req:
            return

        request_id = req.get("id")

        # منع تكرار إرسال نفس الطلب ضمن نافذة قصيرة
        try:
            now_ts = int(time.time())
            last   = _recently_sent.get(request_id)
            if last and (now_ts - last) < _RECENT_TTL:
                return
            _recently_sent[request_id] = now_ts
        except Exception:
            pass

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
                        for admin_id in _admin_targets():
                            bot.send_media_group(admin_id, media)
                    except Exception:
                        logging.exception("Failed to send media group, fallback to message only")
                    for admin_id in _admin_targets():
                        bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
            else:
                for admin_id in _admin_targets():
                    bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)

        # =========== الأنواع الأخرى ===========
        else:
            for admin_id in _admin_targets():
                bot.send_message(admin_id, text, reply_markup=keyboard, parse_mode="HTML")


def queue_cooldown_start(bot=None):
    """إطلاق فترة خمول قصيرة ثم إعادة تشغيل الطابور."""
    global _queue_cooldown
    _queue_cooldown = True

    def release():
        global _queue_cooldown
        time.sleep(30)           # نصف دقيقة
        _queue_cooldown = False
        if bot is not None:
            process_queue(bot)

    threading.Thread(target=release, daemon=True).start()

# نهاية الملف
