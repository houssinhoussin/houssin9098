# services/ads_service.py
from datetime import datetime, timedelta, timezone
from database.db import get_table

TBL = "channel_ads"

def _utc_now():
    return datetime.now(timezone.utc)

def add_channel_ad(
    user_id: int,
    times_total: int,
    price: int,
    contact: str,
    ad_text: str,
    images: list[str] | None = None,
    duration_days: int = 30,
):
    """إنشاء إعلان فعّال يلتقطه السكيجولر تلقائيًا."""
    expire_at = _utc_now() + timedelta(days=duration_days or 30)
    row = {
        "user_id": user_id,
        "ad_text": ad_text or "",
        "images": images or [],
        "contact": contact or "",
        "times_total": int(times_total or 1),
        "times_posted": 0,
        "price": int(price or 0),
        "status": "active",
        "expire_at": expire_at.isoformat(),
    }
    res = get_table(TBL).insert(row).execute()
    data = getattr(res, "data", None) or []
    return data[0] if data else row

def get_active_ads(limit: int = 100):
    """الإعلانات النشطة التي لم تستنفد مرات النشر."""
    q = (
        get_table(TBL)
        .select("*")
        .eq("status", "active")
        .lt("times_posted", "times_total")
        .order("last_posted_at", desc=False, nullsfirst=True)
        .limit(limit)
        .execute()
    )
    return getattr(q, "data", None) or []

def increment_ad_posted(ad_id: int):
    """زيادة العداد وتحديث حالة الانتهاء بأمان."""
    # زيادة times_posted + وضع last_posted_at
    row = get_table(TBL).select("times_posted,times_total").eq("id", ad_id).single().execute().data or {}
    new_count = int(row.get("times_posted", 0)) + 1
    get_table(TBL).update({
        "times_posted": new_count,
        "last_posted_at": _utc_now().isoformat()
    }).eq("id", ad_id).execute()

    # إن اكتمل العدد → Expire
    if new_count >= int(row.get("times_total", 0)):
        get_table(TBL).update({"status": "expired"}).eq("id", ad_id).execute()

def expire_old_ads():
    """تمييز المنتهية زمنياً كـ expired."""
    get_table(TBL).update({"status": "expired"}) \
        .lt("expire_at", _utc_now().isoformat()) \
        .eq("status", "active") \
        .execute()
