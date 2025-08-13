# services/ads_service.py
from __future__ import annotations
from datetime import datetime, timedelta, time, timezone
from typing import List, Dict, Any, Optional
from database.db import get_table
from zoneinfo import ZoneInfo
import math

CHANNEL_ADS_TABLE = "channel_ads"
SYRIA_TZ = ZoneInfo("Asia/Damascus")
WINDOW_START = time(8, 0)   # 08:00
WINDOW_END   = time(22, 0)  # 22:00
WINDOW_SECONDS = (22 - 8) * 3600  # 14h = 50400s

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _now_iso() -> str:
    return _now_utc().isoformat()

def _as_list(images: Optional[List[str]]) -> List[str]:
    if not images:
        return []
    return [str(x) for x in images if str(x).strip()]

def _date_local(dt_iso: Optional[str]) -> Optional[str]:
    if not dt_iso:
        return None
    try:
        d = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
        return d.astimezone(SYRIA_TZ).date().isoformat()
    except Exception:
        return None

def _today_local_iso() -> str:
    return _now_utc().astimezone(SYRIA_TZ).date().isoformat()

def _window_bounds_local(day_iso: Optional[str] = None) -> (datetime, datetime):
    """حدود نافذة النشر اليومية (08:00 -> 22:00) بتوقيت سوريا، تُعاد كـ UTC-aware."""
    if day_iso is None:
        day = _now_utc().astimezone(SYRIA_TZ).date()
    else:
        y, m, d = map(int, day_iso.split("-"))
        day = datetime(y, m, d, tzinfo=SYRIA_TZ).date()
    start_local = datetime.combine(day, WINDOW_START, tzinfo=SYRIA_TZ)
    end_local   = datetime.combine(day, WINDOW_END, tzinfo=SYRIA_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def add_channel_ad(
    user_id: int,
    times_total: int,               # 👈 يُفسَّر كعدد مرات النشر "اليومي"
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
    إنشاء إعلان قناة جديد:
      - times_total = عدد مرات النشر يوميًا داخل نافذة 08:00–22:00 بتوقيت سوريا.
      - times_posted = عدد ما نُشر "في اليوم المحلي" الحالي؛ يُصفّر تلقائيًا عند تغيّر اليوم المحلي.
      - أول نشر يتم فور الموافقة إذا كنّا داخل النافذة، وإلا عند 08:00 المحلي لليوم التالي.
    """
    days = int(duration_days if duration_days is not None else expire_days)
    now = _now_utc()
    payload: Dict[str, Any] = {
        "user_id": int(user_id),
        "times_total": max(1, int(times_total)),  # 👈 عدد يومي
        "times_posted": 0,                        # 👈 عدد اليوم المحلي الحالي
        "price": int(price),
        "contact": (contact or "").strip(),
        "ad_text": ad_text,
        "images": _as_list(images),
        "status": "active",
        "created_at": now.isoformat(),
        "last_posted_at": None,                   # 👈 يسمح بالنشر الأول فورًا داخل النافذة
        "expire_at": (now + timedelta(days=days)).isoformat(),
    }
    return get_table(CHANNEL_ADS_TABLE).insert(payload).execute()

def get_active_ads(limit: int = 200) -> List[Dict[str, Any]]:
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
    تصفير الحصة اليومية عند دخول يوم محلي جديد (Asia/Damascus):
      - times_posted -> 0
      - last_posted_at -> NULL (ليسهل النشر الأول مباشرة داخل النافذة)
    """
    last_day_local = _date_local(ad_row.get("last_posted_at"))
    today_local = _today_local_iso()
    if last_day_local is None:
        # لم يُنشر بعد — لا حاجة لتصفير
        return
    if last_day_local != today_local:
        try:
            get_table(CHANNEL_ADS_TABLE).update({
                "times_posted": 0,
                "last_posted_at": None,
            }).eq("id", ad_id).execute()
        except Exception:
            pass

def _gap_for(ad_row: Dict[str, Any]) -> int:
    """الفاصل المتساوي داخل نافذة 14 ساعة."""
    times_per_day = max(1, int(ad_row.get("times_total") or 1))
    return int(math.floor(WINDOW_SECONDS / times_per_day))

def next_allowed_at(ad_row: Dict[str, Any]) -> datetime:
    """
    يحسب موعد السماح التالي للنشر داخل نافذة 08:00–22:00 (بتوقيت سوريا).
    - أول نشر يومي: فور دخول النافذة (أو فورًا إن كنّا داخل النافذة وlast_posted_at=None).
    - ما بعده: last_posted_at + gap، مع التقيد بحدود النافذة.
    يُعاد التوقيت على شكل UTC datetime.
    """
    now_utc = _now_utc()
    now_local = now_utc.astimezone(SYRIA_TZ)
    today = now_local.date().isoformat()
    win_start_utc, win_end_utc = _window_bounds_local(today)

    # إن لم نكن داخل النافذة الآن
    if now_utc < win_start_utc:
        # أول نشر عند بداية النافذة اليوم
        return win_start_utc
    if now_utc > win_end_utc:
        # خارج النافذة مساءً -> أول نشر عند 08:00 غدًا
        tomorrow_local = (now_local + timedelta(days=1)).date().isoformat()
        next_start_utc, _ = _window_bounds_local(tomorrow_local)
        return next_start_utc

    # داخل النافذة
    last_iso = ad_row.get("last_posted_at")
    if not last_iso:
        # لم يُنشر اليوم بعد -> الآن (للنشر الأول داخل النافذة)
        return now_utc

    # إن وُجد آخر نشر: نحسب gap داخل النافذة
    try:
        last_dt_utc = datetime.fromisoformat(str(last_iso).replace("Z", "+00:00"))
    except Exception:
        last_dt_utc = now_utc - timedelta(seconds=_gap_for(ad_row))

    gap = timedelta(seconds=_gap_for(ad_row))
    candidate = last_dt_utc + gap

    # إن خرج الـ candidate خارج النافذة، نذهب لبداية نافذة اليوم التالي
    if candidate > win_end_utc:
        tomorrow_local = (now_local + timedelta(days=1)).date().isoformat()
        next_start_utc, _ = _window_bounds_local(tomorrow_local)
        return next_start_utc

    # إن كان قبل بداية النافذة الحالية لأي سبب
    if candidate < win_start_utc:
        return win_start_utc

    return candidate

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
            "times_poste_
