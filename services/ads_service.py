# services/ads_service.py
from datetime import datetime, timedelta
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
        "message_ids": None,  # تستطيع لاحقا حفظ الرسائل المنشورة إذا أردت حذفها تلقائيًا
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
