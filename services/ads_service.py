# services/ads_service.py
from datetime import datetime, timedelta
from typing import List, Dict
from database.db import get_table

CHANNEL_ADS_TABLE = "channel_ads"

def add_channel_ad(user_id, times_total, price, contact, ad_text, images):
    now = datetime.utcnow()
    expire_at = now + timedelta(days=5)
    data = {
        "user_id": user_id,
        "times_total": times_total,
        "price": price,
        "contact": contact,
        "ad_text": ad_text,
        "images": images,
        "status": "active",
        "created_at": now.isoformat(),
        "expire_at": expire_at.isoformat(),
        "times_posted": 0,
        "last_posted_at": None,
        "message_ids": None,  # لحفظ معرفات الرسائل المنشورة لاحقًا
    }
    get_table(CHANNEL_ADS_TABLE).insert(data).execute()

def get_active_ads():
    now = datetime.utcnow().isoformat()
    res = (
        get_table(CHANNEL_ADS_TABLE)
        .select("*")
        .eq("status", "active")
        .gt("expire_at", now)
        .execute()
    )
    return res.data if hasattr(res, "data") else []

def increment_ad_posted(ad_id):
    ad = get_table(CHANNEL_ADS_TABLE).select("*").eq("id", ad_id).execute().data[0]
    times_posted = (ad.get("times_posted") or 0) + 1
    status = ad["status"]
    if times_posted >= ad["times_total"]:
        status = "expired"
    get_table(CHANNEL_ADS_TABLE).update({
        "times_posted": times_posted,
        "last_posted_at": datetime.utcnow().isoformat(),
        "status": status,
    }).eq("id", ad_id).execute()

def expire_old_ads():
    now = datetime.utcnow().isoformat()
    get_table(CHANNEL_ADS_TABLE).update({"status": "expired"}).lt("expire_at", now).execute()


# -----------------------------------------------------
# حفظ مراجع رسائل الإعلان المنشورة (chat_id + message_id)
# -----------------------------------------------------
def save_ad_message_refs(ad_id: int, refs: List[Dict[str, int]], append: bool = True) -> None:
    if not refs:
        return

    current: List[Dict[str, int]] = []
    if append:
        sel = (
            get_table(CHANNEL_ADS_TABLE)
            .select("message_ids")
            .eq("id", ad_id)
            .limit(1)
            .execute()
        )
        row = (sel.data or [None])[0]
        if row and isinstance(row.get("message_ids"), list):
            current = row["message_ids"]

    # دمج بدون تكرار
    seen = {(int(x.get("chat_id", 0)), int(x.get("message_id", 0))) for x in current}
    for r in refs:
        key = (int(r.get("chat_id", 0)), int(r.get("message_id", 0)))
        if key not in seen and key[0] and key[1]:
            current.append({"chat_id": key[0], "message_id": key[1]})
            seen.add(key)

    (
        get_table(CHANNEL_ADS_TABLE)
        .update({"message_ids": current})
        .eq("id", ad_id)
        .execute()
    )

# -----------------------------------------------------
# إرجاع مراجع رسائل الإعلان
# -----------------------------------------------------
def get_ad_message_refs(ad_id: int) -> List[Dict[str, int]]:
    sel = (
        get_table(CHANNEL_ADS_TABLE)
        .select("message_ids")
        .eq("id", ad_id)
        .limit(1)
        .execute()
    )
    row = (sel.data or [None])[0]
    msgs = row.get("message_ids") if row else None
    return msgs if isinstance(msgs, list) else []

# -----------------------------------------------------
# حذف الرسائل المنشورة للإعلان (يتطلب صلاحيات في القناة)
# -----------------------------------------------------
def delete_ad_messages(bot, ad_id: int) -> int:
    refs = get_ad_message_refs(ad_id)
    deleted = 0
    for ref in refs:
        chat_id = ref.get("chat_id")
        msg_id = ref.get("message_id")
        if not chat_id or not msg_id:
            continue
        try:
            bot.delete_message(chat_id, msg_id)
            deleted += 1
        except Exception:
            pass
    return deleted
