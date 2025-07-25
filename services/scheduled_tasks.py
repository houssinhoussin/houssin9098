import threading
from services.ads_service import get_active_ads, increment_ad_posted, expire_old_ads
from telebot import types
from datetime import datetime, time as dtime

CHANNEL_USERNAME = "@your_channel"  # غيّر هذا لاسم قناتك

def post_ads_task(bot):
    now = datetime.utcnow()
    # نشر الإعلانات فقط من 10 صباحًا حتى 10 مساءً بتوقيت سوريا (UTC+3)
    syria_now = now + timedelta(hours=3)
    hour = syria_now.hour
    # تحقق من الوقت المناسب للنشر
    if 10 <= hour < 22:
        expire_old_ads()
        ads = get_active_ads()
        for ad in ads:
            if ad["times_posted"] < ad["times_total"]:
                caption = (
                    "🚀✨✨ إعلان مميز من المتجر العالمي ✨✨🚀\n\n"
                    f"{ad['ad_text']}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "📱 للتواصل:\n"
                    f"{ad['contact']}\n"
                    "━━━━━━━━━━━━━━━━━━"
                )
                if ad.get("images"):
                    media = [types.InputMediaPhoto(photo) for photo in ad["images"]]
                    bot.send_media_group(CHANNEL_USERNAME, media)
                bot.send_message(CHANNEL_USERNAME, caption)
                increment_ad_posted(ad["id"])
    # جدولة نفسها كل ساعة
    threading.Timer(3600, post_ads_task, args=(bot,)).start()
