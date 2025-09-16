# -*- coding: utf-8 -*-
from __future__ import annotations
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

from services.referral_service import expire_due_goals
from services.ads_service import (
    get_active_ads,
    refresh_daily_quota,
    next_allowed_at,
    mark_posted,
    expire_old_ads,
    purge_expired_ads,
    latest_global_post_at,
    inside_window_now,
    is_first_service_day_today,
    allowed_times_today,
)

GLOBAL_MIN_GAP_MINUTES = 10  # فاصل عالمي بين أي إعلانين
SYRIA_TZ = ZoneInfo("Asia/Damascus")

# حراس المزايا/الصيانة
try:
    from services.feature_flags import is_feature_enabled
except Exception:  # pragma: no cover
    def is_feature_enabled(_key: str) -> bool:
        return True

try:
    from services.system_service import is_maintenance
except Exception:  # pragma: no cover
    def is_maintenance() -> bool:
        return False

# دالة نشر الإعلان (اختيارية)
try:
    from handlers.ads import publish_channel_ad
except Exception:  # pragma: no cover
    publish_channel_ad = None


def _safe_publish(bot, ad_row) -> bool:
    if publish_channel_ad is None:
        return True
    try:
        return bool(publish_channel_ad(bot, ad_row))
    except Exception as e:
        print(f"[ads_task] publish error for ad {ad_row.get('id')}: {e}")
        return False


def _global_gap_ok() -> bool:
    """يتحقق من مرور 10 دقائق على الأقل منذ آخر نشر عالمي لأي إعلان."""
    last = latest_global_post_at()
    if not last:
        return True
    return (datetime.now(timezone.utc) - last) >= timedelta(minutes=GLOBAL_MIN_GAP_MINUTES)


def _pick_due_ad(now_utc: datetime, ads: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    يختار إعلانًا واحدًا مستحقًا للنشر الآن:
      - يُصفّر الحصة اليومية عند تغير اليوم المحلي.
      - posted_today < allowed_times_today(ad).
      - now >= next_allowed_at(ad).
      - ترتيب: الأقل نشرًا اليوم ثم الأقدم.
    """
    def _key(ad):
        posted = int(ad.get("times_posted") or 0)
        created = ad.get("created_at") or ""
        return (posted, created)

    for ad in sorted(ads, key=_key):
        ad_id = ad.get("id")
        if not ad_id:
            continue

        # تصفير حصة اليوم عند تغيّر اليوم المحلي
        refresh_daily_quota(int(ad_id), ad)

        try:
            allowed = allowed_times_today(ad)
            posted_today = int(ad.get("times_posted") or 0)
        except Exception:
            continue
        if posted_today >= allowed:
            continue

        na = next_allowed_at(ad)
        if now_utc >= na:
            return ad

    return None


# ----- تنظيف خصومات الإحالة المنتهية -----

from database.db import get_table

def expire_old_discounts():
    """تعطيل الخصومات التي انتهى وقتها (active=True && ends_at <= now)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        (
            get_table("discounts")
            .update({"active": False})
            .lte("ends_at", now_iso)
            .eq("active", True)
            .execute()
        )
    except Exception as e:
        print(f"[ads_task] expire_old_discounts error: {e}")


def purge_old_discounts(days: int = 2):
    """حذف سجلات الخصومات المنتهية منذ مدة (اختياري)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        (
            get_table("discounts")
            .delete()
            .lt("ends_at", cutoff)
            .eq("active", False)
            .execute()
        )
    except Exception as e:
        print(f"[ads_task] purge_old_discounts error: {e}")


def post_ads_task(bot=None, every_seconds: int = 60):
    """
    جدولة تعمل كل دقيقة تقريبًا:
      1) تعليم الإعلانات والأهداف المنتهية.
      2) نشر اليوم الأول فورًا داخل النافذة.
      3) اختيار إعلان واحد مستحق مع فاصل 10 دقائق عالمي.
      4) تنظيف الإعلانات المنتهية بعد 14 ساعة.
      5) تعطيل الخصومات المنتهية وحذف القديمة اختياريًا.
    """
    def _tick():
        now_utc = datetime.now(timezone.utc)

        # (1) تعليم المنتهي
        try:
            expire_old_ads()
        except Exception as e:
            print(f"[ads_task] expire_old_ads error: {e}")
        try:
            expire_due_goals()
        except Exception as e:
            print(f"[ads_task] expire_due_goals error: {e}")

        # (2) + (3) النشر
        try:
            if is_maintenance() or (not is_feature_enabled("ads")):
                print("[ads_task] ads disabled or in maintenance; skipping publish tick")
            else:
                ads = get_active_ads(limit=400)

                # اليوم الأول: نشر فوري داخل النافذة
                if inside_window_now():
                    first_day_due = [
                        a for a in ads
                        if is_first_service_day_today(a) and int(a.get("times_posted") or 0) == 0
                    ]
                    for ad in first_day_due:
                        ad_id = ad.get("id")
                        if not ad_id:
                            continue
                        if _safe_publish(bot, ad):
                            mark_posted(int(ad_id))

                # بقية النشرات: التزام بالفاصل العالمي
                if _global_gap_ok():
                    ad = _pick_due_ad(now_utc, ads)
                    if ad is not None:
                        if _safe_publish(bot, ad):
                            mark_posted(int(ad["id"]))
        except Exception as e:
            print(f"[ads_task] main loop error: {e}")

        # (4) تنظيف إعلانات القناة المنتهية
        try:
            removed = purge_expired_ads(hours_after=14)
            if removed:
                print(f"[ads_task] purged expired channel ads: {removed}")
        except Exception as e:
            print(f"[ads_task] purge_expired_ads error: {e}")

        # (5) تعطيل وحذف خصومات منتهية
        expire_old_discounts()
        purge_old_discounts(2)

        # إعادة الجدولة
        threading.Timer(every_seconds, _tick).start()

    # أول تشغيل بعد 10 ثواني لإتاحة تهيئة البوت
    threading.Timer(10, _tick).start()
