# services/scheduled_tasks.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from services.ads_service import (
    get_active_ads,
    refresh_daily_quota,
    next_allowed_at,
    mark_posted,
    expire_old_ads,
    purge_expired_ads,
    latest_global_post_at,
)
from zoneinfo import ZoneInfo

GLOBAL_MIN_GAP_MINUTES = 10  # 👈 فاصل عالمي بين أي إعلانين
SYRIA_TZ = ZoneInfo("Asia/Damascus")

# نحاول استخدام دالة النشر من handlers/ads.py إن وُجدت
try:
    from handlers.ads import publish_channel_ad  # يجب أن ترسل الإعلان حسب الزر/القناة
except Exception:  # pragma: no cover
    publish_channel_ad = None

def _safe_publish(bot, ad_row) -> bool:
    # لو ما في دالة نشر متاحة، نعتبر النشر "نجح" حتى لا تتوقف الجدولة
    if publish_channel_ad is None:
        return True
    try:
        return bool(publish_channel_ad(bot, ad_row))
    except Exception as e:
        print(f"[ads_task] publish error for ad {ad_row.get('id')}: {e}")
        return False

def _global_gap_ok() -> bool:
    """
    يتحقق من مرور 10 دقائق على الأقل منذ آخر نشر عالمي لأي إعلان.
    """
    last = latest_global_post_at()
    if not last:
        return True
    return (datetime.now(timezone.utc) - last) >= timedelta(minutes=GLOBAL_MIN_GAP_MINUTES)

def _pick_due_ad(now_utc: datetime, ads: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    يختار إعلانًا واحدًا “مستحقًا” للنشر الآن:
      - يُصفّر الحصة اليومية عند تغير اليوم المحلي.
      - يتأكد أن posted_today < times_per_day.
      - يتحقق من أن now >= next_allowed_at(ad).
      - يمنع التطابق: نختار أول إعلان مؤهل فقط.
    ترتيب الاختيار: الأقل نشرًا اليوم، ثم الأقدم إنشاءً.
    """
    # إعادة ترتيب: الأقل times_posted أولاً، ثم الأقدم
    def _key(ad):
        posted = int(ad.get("times_posted") or 0)
        created = ad.get("created_at") or ""
        return (posted, created)

    for ad in sorted(ads, key=_key):
        ad_id = ad.get("id")
        if not ad_id:
            continue

        # 1) تصفير الحصة اليومية عند تغيّر اليوم المحلي (داخل سوريا)
        refresh_daily_quota(int(ad_id), ad)

        # 2) حدود الحصة اليومية
        try:
            times_per_day = max(1, int(ad.get("times_total") or 1))
            posted_today = int(ad.get("times_posted")
