# services/ads_service.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from database.db import get_table
import math

CHANNEL_ADS_TABLE = "channel_ads"

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _now_iso() -> str:
    return _now().isoformat()

def _as_list(images: Optional[List[str]]) -> List[str]:
    if not images:
        return []
    return [str(x) for x in images if str(x).strip()]

def _date_only(dt_iso: Optional[str]) -> Optional[str]:
    if not dt_iso:
        return None
    try:
        d = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        return d.date().isoformat()
    except Exception:
        return None

def add_channel_ad(
    user_id: int,
    times_total: int,               # 👈 يُفسَّر الآن كـ "عدد مرات النشر في اليوم"
    price: int,
    contact: Optional[str],
    ad_text: str,
    images: Optional[List[str]] = None,
    *,
    expire_days: int = 5,           # افتراضي 5 أيام
    duration_days: Optional[int] = None,  # توافق مع admin.py
    **_: Any,
):
    """
    إنشاء إعلان قناة جديد.
    ملاحظة مهمة:
      - times_total = عدد مرات النشر يوميًا
      - times_posted = عدد ما نُشر اليوم فقط (يُعاد ضبطه يوميًا)
      - انتهاء الإعلان مضبوط عبر expire_at (افتراضي 5 أيام)
    """
    days = int(duration_days if duration_days is not None else expire_days)
    now = _now()
    payload: Dict[str, Any] = {
        "user_id": int(user_id),
        "times_total": int(times_total),  # 👈 عدد يومي
        "times_posted": 0,                # 👈 عدد اليوم الحالي
        "price": int(price),
        "contact": (contact or "").strip(),
        "ad_text": ad_text,
        "images": _as_list(images),
        "status": "active",
        "created_at": now.isoformat(),
        "last_posted_at": None,           # 👈 يسمح بالنشر فورًا بعد الموافقة
        "expire_at": (now + timedelta(days=days)).isoformat(),
    }
    return get_table(CHANNEL_ADS_TABLE).insert(payload).execute()

def get_active_ads(limit: int = 50) -> List[Dict[str, Any]]:
    """إرجاع الإعلانات النشطة غير المنتهية زمنيًا."""
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
    return getattr(resp, "data", None) or []

def refresh_daily_quota(ad_id: int, ad_row: Dict[str, Any]) -> None:
    """
    إذا دخلنا يومًا جديدًا (UTC) تُصفّر حصة اليوم:
      times_posted -> 0
      last_posted_at -> NULL (ليسهل النشر مباشرة في أول فتحة من اليوم)
    """
    last_day = _date_only(ad_row.get("last_posted_at"))
    today = _now().date().isoformat()
    if last_day is None:
        # لم يُنشر بعد في أي يوم — لا حاجة لتصفير
        return
    if last_day != today:
        try:
            get_table(CHANNEL_ADS_TABLE).update({
                "times_posted": 0,
                "last_posted_at": None,
            }).eq("id", ad_id).execute()
        except Exception:
            pass

def next_allowed_at(ad_row: Dict[str, Any]) -> Optional[datetime]:
    """
    توزيع متساوٍ على مدار اليوم:
      min_gap_seconds = floor(86400 / max(1, times_total))
      يسمح بأول نشر لليوم مباشرة (last_posted_at is NULL)
    """
    times_per_day = max(1, int(ad_row.get("times_total") or 1))
    gap = int(math.floor(86400 / times_per_day))
    last = ad_row.get("last_posted_at")
    if not last:
        return _now()  # مسموح الآن
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except Exception:
        return _now()
    return last_dt + timedelta(seconds=gap)

def mark_posted(ad_id: int) -> None:
    """زيادة عدّاد اليوم وتحديث زمن آخر نشر."""
    try:
        row = (
            get_table(CHANNEL_ADS_TABLE)
            .select("times_posted")
            .eq("id", ad_id)
            .limit(1)
            .execute()
        )
        current = 0
        data = getattr(row, "data", None) or []
        if data:
            current = int(data[0].get("times_posted") or 0)
        get_table(CHANNEL_ADS_TABLE).update({
            "times_posted": current + 1,
            "last_posted_at": _now_iso(),
        }).eq("id", ad_id).execute()
    except Exception:
        pass

def expire_old_ads() -> int:
    """تعليم الإعلانات المنتهية بالحالة expired اعتمادًا على expire_at فقط."""
    now_iso = _now_iso()
    try:
        r = get_table(CHANNEL_ADS_TABLE).update({"status": "expired"}).lt("expire_at", now_iso).execute()
        d = getattr(r, "data", None)
        return len(d) if isinstance(d, list) else 0
    except Exception:
        return 0

def purge_expired_ads(hours_after: int = 14) -> int:
    """حذف الإعلانات بالحالة expired التي مضى على انتهائها أكثر من N ساعة."""
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
