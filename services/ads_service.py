# services/ads_service.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from database.db import get_table

CHANNEL_ADS_TABLE = "channel_ads"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def add_channel_ad(user_id: int, times_total: int, price: int, contact: Optional[str],
                   ad_text: str, images: Optional[List[str]] = None, expire_days: int = 5):
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": int(user_id),
        "times_total": int(times_total),
        "times_posted": 0,
        "price": int(price),
        "contact": (contact or "").strip(),
        "ad_text": ad_text,
        "images": images or [],
        "status": "active",
        "created_at": now.isoformat(),
        "last_posted_at": None,
        "expire_at": (now + timedelta(days=int(expire_days))).isoformat(),
    }
    get_table(CHANNEL_ADS_TABLE).insert(payload).execute()

def get_active_ads() -> List[Dict[str, Any]]:
    now_iso = _now_iso()
    resp = (get_table(CHANNEL_ADS_TABLE)
            .select("*")
            .eq("status", "active")
            .gt("expire_at", now_iso)
            .order("id")
            .execute())
    return getattr(resp, "data", None) or []

def increment_ad_posted(ad_id: int) -> None:
    r = get_table(CHANNEL_ADS_TABLE).select("id, times_total, times_posted, expire_at, status").eq("id", ad_id).limit(1).execute()
    rows = getattr(r, "data", None) or []
    if not rows:
        return
    ad = rows[0]
    times_total = int(ad.get("times_total") or 0)
    times_posted = int(ad.get("times_posted") or 0) + 1
    status = ad.get("status") or "active"
    if times_posted >= times_total:
        status = "expired"
    try:
        if ad.get("expire_at") and ad["expire_at"] <= _now_iso():
            status = "expired"
    except Exception:
        pass
    get_table(CHANNEL_ADS_TABLE).update({
        "times_posted": times_posted, "last_posted_at": _now_iso(), "status": status,
    }).eq("id", ad_id).execute()

def expire_old_ads() -> int:
    now_iso = _now_iso()
    resp = get_table(CHANNEL_ADS_TABLE).update({"status": "expired"}).lt("expire_at", now_iso).execute()
    data = getattr(resp, "data", None)
    return len(data) if isinstance(data, list) else 0

# ✅ جديد: حذف الإعلانات المنتهية بعد انتهاء الإعلان بمدة (افتراضي 14 ساعة)
def purge_expired_ads(hours_after: int = 14) -> int:
    """
    حذف الإعلانات المنتهية من جدول القناة بعد انتهاء المدة.
    - يُحذف أي صف status='expired' وكانت آخر_عملية نشر last_posted_at أقدم من N ساعة
      أو تاريخ الانتهاء expire_at أقدم من N ساعة.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_after)
    cutoff_iso = cutoff.isoformat()
    total = 0
    # حسب last_posted_at
    try:
        r1 = (
            get_table(CHANNEL_ADS_TABLE)
            .delete()
            .eq("status", "expired")
            .lte("last_posted_at", cutoff_iso)
            .execute()
        )
        d1 = getattr(r1, "data", None)
        total += len(d1) if isinstance(d1, list) else 0
    except Exception:
        pass
    # أو حسب expire_at
    try:
        r2 = (
            get_table(CHANNEL_ADS_TABLE)
            .delete()
            .eq("status", "expired")
            .lte("expire_at", cutoff_iso)
            .execute()
        )
        d2 = getattr(r2, "data", None)
        total += len(d2) if isinstance(d2, list) else 0
    except Exception:
        pass
    return total
