# services/scheduled_tasks.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import threading
from datetime import datetime, timezone
from typing import Optional
from services.ads_service import (
    get_active_ads,
    refresh_daily_quota,
    next_allowed_at,
    mark_posted,
    expire_old_ads,
)
from services.ads_service import purge_expired_ads

# نحاول استخدام دالة النشر من handlers/ads.py إن وُجدت
try:
    from handlers.ads import publish_channel_ad  # يجب أن ترسل الإعلان حسب الزر/القناة
except Exception:  # pragma: no cover
    publish_channel_ad = None

def _safe_publish(bot, ad_row) -> bool:
    # لو ما في دالة نشر متاحة، نعتبر النشر "نجح" لتفادي توقف الجدولة (يمكنك استبدالها بدالتك)
    if publish_channel_ad is None:
        return True
    try:
        return bool(publish_channel_ad(bot, ad_row))
    except Exception as e:
        print(f"[ads_task] publish error for ad {ad_row.get('id')}: {e}")
        return False

def post_ads_task(bot=None, every_seconds: int = 60):
    """
    جدولة تقوم كل دقيقة تقريبًا:
      1) تعليم المنتهي expired.
      2) لكل إعلان نشط: تصفير حصة اليوم إن بدأ يوم جديد (UTC).
      3) توزيع النشر اليومي بشكل متساوٍ على مدار 24 ساعة.
         - times_total = عدد مرات اليوم
         - times_posted = عدد ما نُشر اليوم
         - أول نشر فورًا بعد الموافقة لكون last_posted_at=None
    """
    def _tick():
        try:
            expire_old_ads()
        except Exception as e:
            print(f"[ads_task] expire_old_ads error: {e}")

        try:
            ads = get_active_ads(limit=200)
            now = datetime.now(timezone.utc)
            for ad in ads:
                ad_id = ad.get("id")
                if not ad_id:
                    continue

                # 1) تصفير حصة اليوم عند تغيّر اليوم
                refresh_daily_quota(int(ad_id), ad)

                # 2) قرارات النشر
                try:
                    times_per_day = max(1, int(ad.get("times_total") or 1))
                    posted_today = int(ad.get("times_posted") or 0)
                except Exception:
                    continue

                if posted_today >= times_per_day:
                    # اكتملت حصة اليوم؛ ننتظر اليوم التالي
                    continue

                na = next_allowed_at(ad)
                if na and now >= na:
                    # يُنشر الآن
                    if _safe_publish(bot, ad):
                        mark_posted(int(ad_id))
        except Exception as e:
            print(f"[ads_task] main loop error: {e}")

        # تنظيف الإعلانات المنتهية بعد 14 ساعة
        try:
            removed = purge_expired_ads(hours_after=14)
            if removed:
                print(f"[ads_task] purged expired channel ads: {removed}")
        except Exception as e:
            print(f"[ads_task] purge_expired_ads error: {e}")

        # إعادة الجدولة
        threading.Timer(every_seconds, _tick).start()

    # أول تشغيل بعد 10 ثواني لإتاحة تهيئة البوت
    threading.Timer(10, _tick).start()
