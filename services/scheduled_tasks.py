import threading
from datetime import datetime, timedelta
from telebot import types
from services.ads_service import get_active_ads, increment_ad_posted, expire_old_ads

CHANNEL_USERNAME = "@my_fast_shop_bot"  # غيّر هذا لاسم قناتك

def post_ads_task(bot):
    now = datetime.utcnow()
    # توقيت سوريا (UTC+3)
    syria_now = now + timedelta(hours=3)
    hour = syria_now.hour
    # نشر الإعلانات من 10 صباحًا حتى 10 مساءً
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
                images = ad.get("images", [])
                if images:
                    if len(images) == 1:
                        bot.send_photo(CHANNEL_USERNAME, images[0], caption=caption)
                    else:
                        media = [types.InputMediaPhoto(photo) for photo in images]
                        media[0].caption = caption
                        bot.send_media_group(CHANNEL_USERNAME, media)
                else:
                    bot.send_message(CHANNEL_USERNAME, caption)
                increment_ad_posted(ad["id"])
    # جدولة نفسها كل ساعة دائماً
    threading.Timer(3600, post_ads_task, args=(bot,)).start()
