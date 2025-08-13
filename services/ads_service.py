# services/ads_service.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from database.db import get_table

CHANNEL_ADS_TABLE = "channel_ads"

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _now_iso() -> str:
    return _now().isoformat()

def _as_list(images: Optional[List[str]]) -> List[str]:
    if not images:
        return []
    # فلترة القيم الفارغة
    return [str(x) for x in images if str(x).strip()]

def add_channel_ad(
    user_id: int,
    times_total: int,
    price: int,
    contact: Optional[str],
    ad_text: str,
    images: Optional[List[str]] = None,
    *,
    expire_days: int = 5,
    # ✅ لقب قديم مستخدم في admin.py — نجعله متوافقًا
    duration_days: Optional[int] = None,
    **_: Any,
):
    """إنشاء إعلان قناة جديد.
    يقبل كلًّا من ``expire_days`` و ``duration_days`` (تُغلّب ``duration_days`` إن وُجدت)
    لتفادي كسر التوافق مع الاستدعاءات القديمة في handlers/admin.py.
    """
    days = int(duration_days if duration_days is not None else expire_days)
    now = _now()
    payload: Dict[str, Any] = {
        "user_id": int(user_id),
        "times_total": int(times_total),
        "times_posted": 0,
        "price": int(price),
        "contact": (contact or "").strip(),
        "ad_text": ad_text,
        "images": _as_list(images),
        "status": "active",
        "created_at": now.isoformat(),
        "last_posted_at": None,
        "expire_at": (now + timedelta(days=days)).isoformat(),
    }
    return get_table(CHANNEL_ADS_TABLE).insert(payload).execute()

def get_active_ads(limit: int = 20) -> List[Dict[str, Any]]:
    """إرجاع الإعلانات النشطة التي لم تنتهِ، مع تفضيل غير المنشورة بعد."""
    now_iso = _now_iso()
    resp = (
        get_table(CHANNEL_ADS_TABLE)
        .select("*")
        .eq("status", "active")
        .gt("expire_at", now_iso)
        .order("last_posted_at", desc=False, nullsfirst=True)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    # استبعاد من استُهلك بالكامل
    return [r for r in rows if int(r.get("times_posted") or 0) < int(r.get("times_total") or 0)]

def increment_ad_posted(ad_id: int) -> None:
    """تحديث عدّاد النشر وتعديل الحالة تلقائيًا."""
    r = get_table(CHANNEL_ADS_TABLE).select("id,times_total,times_posted,expire_at,status").eq("id", ad_id).limit(1).execute()
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
        "times_posted": times_posted,
        "last_posted_at": _now_iso(),
        "status": status,
    }).eq("id", ad_id).execute()

def expire_old_ads() -> int:
    """تعليم الإعلانات المنتهية بالحالة expired إما لانتهاء الوقت أو استهلاك العدد."""
    now_iso = _now_iso()
    total = 0
    # حسب الوقت
    try:
        r1 = get_table(CHANNEL_ADS_TABLE).update({"status": "expired"}).lt("expire_at", now_iso).execute()
        d1 = getattr(r1, "data", None)
        total += len(d1) if isinstance(d1, list) else 0
    except Exception:
        pass
    # حسب استهلاك العدد (قد لا تدعم بعض واجهات PostgREST مقارنة عمود بعمود؛ إن فشلت نتجاهل)
    try:
        r2 = (
            get_table(CHANNEL_ADS_TABLE)
            .update({"status": "expired"})
            .gte("times_posted", "times_total")
            .execute()
        )
        d2 = getattr(r2, "data", None)
        total += len(d2) if isinstance(d2, list) else 0
    except Exception:
        pass
    return total

def purge_expired_ads(hours_after: int = 14) -> int:
    """حذف الإعلانات التي أصبحت بالحالة expired منذ أكثر من N ساعة."""
    cutoff_iso = (_now() - timedelta(hours=int(hours_after))).isoformat()
    total = 0
    # حسب آخر نشر
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
    # أو حسب انتهاء الوقت
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
