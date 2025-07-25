import threading
from services.ads_service import get_active_ads, increment_ad_posted, expire_old_ads
from telebot import types

CHANNEL_USERNAME = "@your_channel"  # غيره لاسم قناتك

def post_ads_task(bot):
    from datetime import datetime
    now = datetime.utcnow()
    expire_old_ads()

    ads = get_active_ads()
    for ad in ads:
        # نشر فقط إذا بقي له نشرات ولم ينته اليوم
        if ad["times_posted"] < ad["times_total"]:
            caption = (
                "🚀✨✨ إعلان مميز من المتجر العالمي ✨✨🚀\n\n"
                f"{ad['ad_text']}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "📱 للتواصل:\n"
                f"{ad['contact']}\n"
                "━━━━━━━━━━━━━━━━━━"
            )
            # نشر الصور إذا فيه
            if ad.get("images"):
                media = [types.InputMediaPhoto(photo) for photo in ad["images"]]
                msg_group = bot.send_media_group(CHANNEL_USERNAME, media)
                # حفظ آي دي الرسائل لو أردت لاحقًا حذفها
            msg = bot.send_message(CHANNEL_USERNAME, caption)
            increment_ad_posted(ad["id"])
    # إعادة جدولة نفسها كل ساعة
    threading.Timer(3600, post_ads_task, args=(bot,)).start()
