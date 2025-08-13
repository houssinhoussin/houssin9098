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
        "ad_text": ad_text or "",
        "images": images or [],
        "contact": (contact or "").strip() or None,
        "times_total": int(times_total),
        "times_posted": 0,
        "price": int(price),
        "status": "active",
        "created_at": now.isoformat(),
        "last_posted_at": None,
        "expire_at": (now + timedelta(days=expire_days)).isoformat(),
    }
    return get_table(CHANNEL_ADS_TABLE).insert(payload).execute()

def get_active_ads(limit: int = 20):
    now_iso = _now_iso()
    resp = (get_table(CHANNEL_ADS_TABLE)
            .select("*").eq("status", "active").gt("expire_at", now_iso)
            .order("last_posted_at", desc=False, nullsfirst=True)
            .order("created_at", desc=False).limit(limit).execute())
    rows = getattr(resp, "data", None) or []
    return [r for r in rows if int(r.get("times_posted") or 0) < int(r.get("times_total") or 0)]

def increment_ad_posted(ad_id: int):
    resp = get_table(CHANNEL_ADS_TABLE).select("*").eq("id", ad_id).limit(1).execute()
    data = getattr(resp, "data", None) or []
    if not data: return
    ad = data[0]
    times_posted = int(ad.get("times_posted") or 0) + 1
    times_total  = int(ad.get("times_total")  or 0)
    status = "active"
    if times_posted >= times_total: status = "expired"
    try:
        if ad.get("expire_at") and ad["expire_at"] <= _now_iso():
            status = "expired"
    except Exception: pass
    get_table(CHANNEL_ADS_TABLE).update({
        "times_posted": times_posted, "last_posted_at": _now_iso(), "status": status,
    }).eq("id", ad_id).execute()

def expire_old_ads() -> int:
    now_iso = _now_iso()
    resp = get_table(CHANNEL_ADS_TABLE).update({"status": "expired"}).lt("expire_at", now_iso).execute()
    data = getattr(resp, "data", None)
    return len(data) if isinstance(data, list) else 0
