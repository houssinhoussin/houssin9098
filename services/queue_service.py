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
from postgrest.exceptions import APIError  # ← لالتقاط 23505 وقت السباق

QUEUE_TABLE = "pending_requests"

_queue_lock = threading.Lock()
_queue_cooldown = False

# منع التكرار ضمن نافذة قصيرة (حماية إضافية من تعدد الاستدعاءات)
_recently_sent = {}        # {request_id: last_ts}
_RECENT_TTL     = 40       # ثوانٍ

def _admin_targets():
    # إرجاع قائمة الإداريين (ADMINS + ADMIN_MAIN_ID) بدون تكرار، مع الحفاظ على الترتيب.
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

# حد أقصى آمن لكابتشن الصور
_MAX_CAPTION = 900

def _add_pending_request_text(user_id: int, username: str, request_text: str, payload=None):
    """
    يعيد:
      - {"status": "created", "request_id": int} عند إنشاء سجل جديد
      - {"status": "duplicate", "request_id": int} إذا لدى المستخدم طلب قيد المعالجة
      - {"status": "error"} عند فشل نهائي (شبكي)
    """
    # 1) فحص وجود سابق لنفس user_id
    try:
        exists = (
            get_table(QUEUE_TABLE)
            .select("id, created_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if exists.data:
            # رفض طلب جديد — موجود سابقًا
            return {"status": "duplicate", "request_id": exists.data[0]["id"]}
    except Exception as e:
        logging.warning("pre-check pending exists failed (will continue): %s", e)

    # 2) محاولة الإدراج مع إعادة المحاولة للشبكة + التقاط 23505
    data = {
        "user_id": user_id,
        "username": username,
        "request_text": request_text,
        "created_at": datetime.utcnow().isoformat()
    }
    if payload is not None:
        data["payload"] = payload

    for attempt in range(1, 4):
        try:
            r = get_table(QUEUE_TABLE).insert(data).execute()
            rid = (r.data or [{}])[0].get("id")
            return {"status": "created", "request_id": rid}
        except APIError as e:
            # لو حدث سباق وأرجعت القاعدة 23505 نرجع duplicate بهدوء
            code = getattr(e, "code", None)
            if code == "23505":
                try:
                    ex = (
                        get_table(QUEUE_TABLE)
                        .select("id")
                        .eq("user_id", user_id)
                        .limit(1)
                        .execute()
                    )
                    if ex.data:
                        return {"status": "duplicate", "request_id": ex.data[0]["id"]}
                except Exception:
                    pass
                return {"status": "duplicate", "request_id": None}
            # غير ذلك: خطأ من PostgREST — لا نعيد رفعه حتى لا ينهار الworker
            logging.exception("[QUEUE] APIError on insert: %s", e)
            return {"status": "error"}
        except httpx.ReadError as e:
            logging.warning(f"Attempt {attempt}: ReadError in add_pending_request: {e}")
            time.sleep(0.5)

    logging.error(f"Failed to add pending request for user {user_id} after 3 attempts.")
    return {"status": "error"}

def add_pending_request(*args, **kwargs):
    """
    واجهة متوافقة ترسل طلبًا إلى طابور الإدمن.
    - النمط القديم: add_pending_request(user_id, username, request_text, payload=None)
    - النمط الجديد: add_pending_request(user_id=..., action="...", payload={...}, approve_channel="admin", meta={...})
    تُعيد dict يوضح الحالة كما في _add_pending_request_text.
    """
    # النمط الجديد بكلمات مفتاحية
    if "action" in kwargs or ("user_id" in kwargs and "payload" in kwargs and "approve_channel" in kwargs):
        user_id = int(kwargs.get("user_id", 0))
        action = str(kwargs.get("action", "request"))
        payload = kwargs.get("payload") or {}
        meta    = kwargs.get("meta") or {}
        username = meta.get("username") or meta.get("from") or "system"
        # نص افتراضي مقروء
        request_text = meta.get("text") or f"طلب إداري: {action}"
        return _add_pending_request_text(user_id, username, request_text, payload)
    # النمط القديم بالمواقع
    if len(args) >= 3:
        user_id, username, request_text = args[:3]
        payload = args[3] if len(args) >= 4 else kwargs.get("payload")
        return _add_pending_request_text(int(user_id), str(username), str(request_text), payload)
    # محاولة أخيرة من kwargs
    if set(kwargs.keys()) >= {"user_id","username","request_text"}:
        return _add_pending_request_text(int(kwargs["user_id"]), str(kwargs["username"]), str(kwargs["request_text"]), kwargs.get("payload"))
    raise TypeError("add_pending_request: استخدم النمط القديم (user_id, username, request_text, payload) أو الجديد (user_id=.., action=.., payload=..).")


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

def _payload_get(request_id: int):
    try:
        r = get_table(QUEUE_TABLE).select("payload").eq("id", request_id).single().execute()
        return (r.data or {}).get("payload") or {}
    except Exception:
        return {}

def _payload_update(request_id: int, patch: dict):
    try:
        old = _payload_get(request_id)
        newp = dict(old)
        newp.update(patch or {})
        get_table(QUEUE_TABLE).update({"payload": newp}).eq("id", request_id).execute()
    except Exception:
        logging.exception("payload update failed for request %s", request_id)

def postpone_request(request_id: int):
    # إرجاع الطلب لآخر الدور بتحديث created_at + إزالة القفل + مسح كاش التكرار.
    try:
        now = datetime.utcnow().isoformat()
        get_table(QUEUE_TABLE).update({"created_at": now}).eq("id", request_id).execute()
        _payload_update(request_id, {"locked_by": None, "locked_by_username": None})
        reset_recent_silently(request_id)  # مهم: السماح بإعادة الإرسال بعد التأجيل
    except Exception:
        logging.exception(f"Error postponing request {request_id}")


def _send_admin_with_photo(bot, photo_id: str, text: str, keyboard: InlineKeyboardMarkup):
    # يرسل صورة/رسالة لكل الأدمن ويُعيد قائمة [(admin_id, message_id)] للرسائل ذات الأزرار.
    sent = []
    try:
        if text and len(text) <= _MAX_CAPTION:
            for admin_id in _admin_targets():
                m = bot.send_photo(admin_id, photo_id, caption=text, parse_mode="HTML", reply_markup=keyboard)
                try:
                    sent.append((admin_id, m.message_id))
                except Exception:
                    pass
        else:
            for admin_id in _admin_targets():
                bot.send_photo(admin_id, photo_id, caption="🖼️ تفاصيل الطلب في الرسالة التالية ⬇️", parse_mode="HTML")
                m = bot.send_message(admin_id, text or "طلب جديد", parse_mode="HTML", reply_markup=keyboard)
                try:
                    sent.append((admin_id, m.message_id))
                except Exception:
                    pass
    except Exception:
        logging.exception("Failed sending admin photo/message; falling back to text-only")
        for admin_id in _admin_targets():
            m = bot.send_message(admin_id, text or "طلب جديد", parse_mode="HTML", reply_markup=keyboard)
            try:
                sent.append((admin_id, m.message_id))
            except Exception:
                pass
    return sent

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
            InlineKeyboardButton("📌 استلمت", callback_data=f"admin_queue_claim_{request_id}"),
            InlineKeyboardButton("🔁 تأجيل",  callback_data=f"admin_queue_postpone_{request_id}"),
            InlineKeyboardButton("✅ تأكيد",   callback_data=f"admin_queue_accept_{request_id}"),
            InlineKeyboardButton("🚫 إلغاء",  callback_data=f"admin_queue_cancel_{request_id}"),
            InlineKeyboardButton("✉️ رسالة للعميل", callback_data=f"admin_queue_message_{request_id}"),
            InlineKeyboardButton("🖼️ صورة للعميل", callback_data=f"admin_queue_photo_{request_id}")
        )

        payload  = req.get("payload") or {}
        typ      = payload.get("type")
        photo_id = payload.get("photo")

        sent_pairs = []  # [(admin_id, message_id)]

        # =========== فرع شحن المحفظة ===========
        if typ == "recharge" and photo_id:
            sent_pairs = _send_admin_with_photo(bot, photo_id, text, keyboard)

        # =========== فرع إعلانات القناة ===========
        elif typ == "ads":
            images = payload.get("images", [])
            if images:
                if len(images) == 1:
                    sent_pairs = _send_admin_with_photo(bot, images[0], text, keyboard)
                else:
                    try:
                        media = [InputMediaPhoto(fid) for fid in images]
                        for admin_id in _admin_targets():
                            bot.send_media_group(admin_id, media)
                    except Exception:
                        logging.exception("Failed to send media group, fallback to message only")
                    for admin_id in _admin_targets():
                        m = bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
                        try:
                            sent_pairs.append((admin_id, m.message_id))
                        except Exception:
                            pass
            else:
                for admin_id in _admin_targets():
                    m = bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
                    try:
                        sent_pairs.append((admin_id, m.message_id))
                    except Exception:
                        pass

        # =========== الأنواع الأخرى ===========
        else:
            for admin_id in _admin_targets():
                m = bot.send_message(admin_id, text, reply_markup=keyboard, parse_mode="HTML")
                try:
                    sent_pairs.append((admin_id, m.message_id))
                except Exception:
                    pass

        # حفظ admin_msgs مع تفريغ القفل
        try:
            entries = [{'admin_id': aid, 'message_id': mid} for (aid, mid) in sent_pairs if aid and mid]
            # لاحظ: نبقي payload الأخرى كما هي ونضيف/نحدث admin_msgs والقفل
            old = _payload_get(request_id)
            old['admin_msgs'] = entries
            old['locked_by'] = None
            old['locked_by_username'] = None
            get_table(QUEUE_TABLE).update({"payload": old}).eq("id", request_id).execute()
        except Exception:
            logging.exception("Failed to persist admin message IDs for request %s", request_id)

def queue_cooldown_start(bot=None):
    # إطلاق فترة خمول قصيرة ثم إعادة تشغيل الطابور.
    global _queue_cooldown
    _queue_cooldown = True

    def release():
        global _queue_cooldown
        time.sleep(30)           # نصف دقيقة
        _queue_cooldown = False
        if bot is not None:
            process_queue(bot)

    threading.Thread(target=release, daemon=True).start()


def reset_recent_silently(request_id: int):
    """
    ينسف كاش منع التكرار لطلب معيّن حتى يُسمَح بإعادة إرساله فورًا عند التأجيل.
    يستخدم داخل postpone_request.
    """
    try:
        _recently_sent.pop(request_id, None)
    except Exception:
        pass
