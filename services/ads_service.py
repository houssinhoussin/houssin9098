# services/ads_service.py
from __future__ import annotations
from datetime import datetime, timedelta, time, timezone, date
from typing import List, Dict, Any, Optional, Tuple
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

def _to_dt(dt_iso: Optional[str]) -> Optional[datetime]:
    if not dt_iso:
        return None
    try:
        return datetime.fromisoformat(str(dt_iso).replace("Z", "+00:00"))
    except Exception:
        return None

def _local_date(dt_iso: Optional[str]) -> Optional[date]:
    dt = _to_dt(dt_iso)
    if not dt:
        return None
    return dt.astimezone(SYRIA_TZ).date()

def _local_time(dt_iso: Optional[str]) -> Optional[time]:
    dt = _to_dt(dt_iso)
    if not dt:
        return None
    return dt.astimezone(SYRIA_TZ).time()

def _today_local_date() -> date:
    return _now_utc().astimezone(SYRIA_TZ).date()

def _window_bounds_local(day_iso: Optional[str] = None) -> Tuple[datetime, datetime]:
    """حدود نافذة النشر اليومية (08:00 -> 22:00) بتوقيت سوريا، تُعاد كتوقيت UTC-aware."""
    if day_iso is None:
        day = _today_local_date()
    else:
        y, m, d = map(int, day_iso.split("-"))
        day = date(y, m, d)
    start_local = datetime.combine(day, WINDOW_START, tzinfo=SYRIA_TZ)
    end_local   = datetime.combine(day, WINDOW_END, tzinfo=SYRIA_TZ)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

def inside_window_now() -> bool:
    now = _now_utc()
    start_utc, end_utc = _window_bounds_local(_today_local_date().isoformat())
    return start_utc <= now <= end_utc

def first_service_day(ad_row: Dict[str, Any]) -> date:
    """
    اليوم الذي نعتبره "اليوم الأول" فعلاً:
      - إذا كانت الموافقة/الإنشاء داخل النافذة (<=22:00) ⇒ اليوم نفسه.
      - إذا بعد 22:00 ⇒ اليوم التالي (أول نشر عند 08:00).
    """
    created_local_time = _local_time(ad_row.get("created_at"))
    created_local_date = _local_date(ad_row.get("created_at")) or _today_local_date()
    if created_local_time and created_local_time > WINDOW_END:
        return created_local_date + timedelta(days=1)
    return created_local_date

def is_first_service_day_today(ad_row: Dict[str, Any]) -> bool:
    return _today_local_date() == first_service_day(ad_row)

def allowed_times_today(ad_row: Dict[str, Any]) -> int:
    """
    في اليوم الأول نسمح بنشرة واحدة فقط.
    من اليوم الثاني فصاعدًا: العدد اليومي المختار.
    """
    if is_first_service_day_today(ad_row):
        return 1
    return max(1, int(ad_row.get("times_total") or 1))

def add_channel_ad(
    user_id: int,
    times_total: int,               # 👈 يُفسَّر كعدد مرات النشر "اليومي" (من اليوم الثاني فصاعدًا)
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
      - اليوم الأول: نشر واحد فور الموافقة إذا كنا داخل نافذة سوريا 08:00–22:00.
      - من اليوم الثاني: times_total مرات يوميًا داخل النافذة، مع توزيع متساوٍ.
    """
    days = int(duration_days if duration_days is not None else expire_days)
    now = _now_utc()
    payload: Dict[str, Any] = {
        "user_id": int(user_id),
        "times_total": max(1, int(times_total)),
        "times_posted": 0,                 # عدّاد نشرات "اليوم المحلي" الحالي
        "price": int(price),
        "contact": (contact or "").strip(),
        "ad_text": ad_text,
        "images": _as_list(images),
        "status": "active",
        "created_at": now.isoformat(),
        "last_posted_at": None,            # يسمح بالنشر الأول فورًا داخل النافذة
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
      - last_posted_at -> NULL (ليسهل النشر الأول داخل النافذة)
    """
    last_day_local = _local_date(ad_row.get("last_posted_at"))
    today_local = _today_local_date()
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
    """الفاصل المتساوي داخل نافذة 14 ساعة، بحسب الحصة المسموحة لليوم."""
    times_per_day = max(1, int(allowed_times_today(ad_row)))
    return int(math.floor(WINDOW_SECONDS / times_per_day))

def next_allowed_at(ad_row: Dict[str, Any]) -> datetime:
    """
    يحسب موعد السماح التالي للنشر داخل نافذة 08:00–22:00 (بتوقيت سوريا).
    - اليوم الأول: بما أننا نسمح بنشرة واحدة فقط، سيمنع الشرط في جدولة الاختيار أي نشرات إضافية.
    """
    now_utc = _now_utc()
    now_local_day = _today_local_date().isoformat()
    win_start_utc, win_end_utc = _window_bounds_local(now_local_day)

    # إذا كنا خارج النافذة الآن
    if now_utc < win_start_utc:
        return win_start_utc
    if now_utc > win_end_utc:
        # أول نشر لليوم التالي عند 08:00
        tomorrow_local = (_today_local_date() + timedelta(days=1)).isoformat()
        next_start_utc, _ = _window_bounds_local(tomorrow_local)
        return next_start_utc

    # داخل النافذة
    last_iso = ad_row.get("last_posted_at")
    if not last_iso:
        # لم يُنشر اليوم بعد -> الآن (سيتحقق المنع/السماح خارج هذه الدالة)
        return now_utc

    # إن وُجد آخر نشر: gap داخل النافذة
    last_dt_utc = _to_dt(last_iso) or (now_utc - timedelta(seconds=_gap_for(ad_row)))
    gap = timedelta(seconds=_gap_for(ad_row))
    candidate = last_dt_utc + gap

    # إن خرج المرشح خارج النافذة، نذهب لبداية نافذة اليوم التالي
    if candidate > win_end_utc:
        tomorrow_local = (_today_local_date() + timedelta(days=1)).isoformat()
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
    cutoff_iso = (_now_utc() - timedelta(hours=int(hours_after))).isoformat()
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

def latest_global_post_at() -> Optional[datetime]:
    """آخر وقت نشر لأي إعلان (لاستخدامه كفاصل عالمي 10 دقائق)."""
    try:
        r = (
            get_table(CHANNEL_ADS_TABLE)
            .select("last_posted_at")
            .order("last_posted_at", desc=True, nullsfirst=False)
            .limit(1)
            .execute()
        )
        rows = getattr(r, "data", None) or []
        if not rows:
            return None
        v = rows[0].get("last_posted_at")
        if not v:
            return None
        return _to_dt(v)
    except Exception:
        return None
