# services/scheduled_tasks.py
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telebot import types
from services.ads_service import get_active_ads, increment_ad_posted, expire_old_ads
from config import CHANNEL_USERNAME

def post_ads_task(bot):
    syria_now = datetime.now(ZoneInfo("Asia/Damascus"))
    hour = syria_now.hour

    # نافذة النشر: 9 صباحًا → 10 مساءً (22)
    if 9 <= hour < 22:
        expire_old_ads()
        ads = get_active_ads()

        for ad in ads:
            # لا تنشر أكثر من مرة خلال ساعة لنفس الإعلان
            last = ad.get("last_posted_at")
            if last:
                try:
                    last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if (datetime.utcnow() - last_dt).total_seconds() < 3600:
                        continue
                except Exception:
                    pass

            caption = (
                "📣 <b>إعلان مدفوع</b>\n\n"
                f"{ad.get('ad_text','')}\n\n"
                "━━━━━━━━━━━━\n"
                f"📞 للتواصل: {ad.get('contact','-')}"
            )

            images = ad.get("images") or []
            try:
                if images:
                    if len(images) == 1:
                        bot.send_photo(CHANNEL_USERNAME, images[0], caption=caption, parse_mode="HTML")
                    else:
                        media = [types.InputMediaPhoto(p) for p in images]
                        media[0].caption = caption
                        bot.send_media_group(CHANNEL_USERNAME, media)
                else:
                    bot.send_message(CHANNEL_USERNAME, caption, parse_mode="HTML")

                increment_ad_posted(ad["id"])
            except Exception:
                # تجاهل الإعلان الذي فشل نشره في هذه الدورة
                continue

    # إعادة التشغيل بعد ساعة دائمًا
    threading.Timer(3600, post_ads_task, args=(bot,)).start()
