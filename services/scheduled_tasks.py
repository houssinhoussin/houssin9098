# services/scheduled_tasks.py
import threading
import logging
from datetime import datetime, timedelta
from telebot import types

from services.ads_service import (
    get_active_ads,
    increment_ad_posted,
    expire_old_ads,
    save_ad_message_refs,   # ← جديد: حفظ message_ids
)
from config import CHANNEL_USERNAME

def post_ads_task(bot):
    """
    ينشر الإعلانات الفعّالة في القناة ضمن نافذة زمنية (10:00–22:00 بتوقيت سوريا)،
    يحفظ message_ids لكل إعلان، ويزيد العداد times_posted.
    يعاد جدولة نفسه كل ساعة.
    """
    try:
        now_utc = datetime.utcnow()
        syria_now = now_utc + timedelta(hours=3)  # تعويض فرق التوقيت
        hour = syria_now.hour

        if 10 <= hour < 22:
            # انتهِ من أي إعلانات منتهية قبل جلب القائمة
            try:
                expire_old_ads()
            except Exception as e:
                logging.warning(f"[ADS] expire_old_ads failed: {e}")

            ads = []
            try:
                ads = get_active_ads() or []
            except Exception as e:
                logging.error(f"[ADS] get_active_ads failed: {e}", exc_info=True)

            for ad in ads:
                try:
                    ad_id = ad.get("id")
                    times_posted = int(ad.get("times_posted") or 0)
                    times_total  = int(ad.get("times_total") or 0)

                    if times_total and times_posted >= times_total:
                        continue  # اكتمل هذا الإعلان

                    caption = (
                        "🚀✨✨ إعلان مميز من المتجر العالمي ✨✨🚀\n\n"
                        f"{ad.get('ad_text','')}\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "📱 للتواصل:\n"
                        f"{ad.get('contact','')}\n"
                        "━━━━━━━━━━━━━━━━━━"
                    )

                    images = ad.get("images") or []
                    saved_refs = []

                    # النشر
                    if images:
                        if len(images) == 1:
                            sent = bot.send_photo(CHANNEL_USERNAME, images[0], caption=caption)
                            # pyTelegramBotAPI يعيد Message واحد
                            saved_refs.append({"chat_id": CHANNEL_USERNAME, "message_id": sent.message_id})
                        else:
                            # مجموعة صور: اجعل الكابشن في أول عنصر
                            media = [types.InputMediaPhoto(photo) for photo in images]
                            # إضافة caption لأول عنصر فقط
                            media[0].caption = caption
                            # ترجع قائمة Messages
                            sent_list = bot.send_media_group(CHANNEL_USERNAME, media)
                            for m in sent_list:
                                saved_refs.append({"chat_id": CHANNEL_USERNAME, "message_id": m.message_id})
                    else:
                        sent = bot.send_message(CHANNEL_USERNAME, caption)
                        saved_refs.append({"chat_id": CHANNEL_USERNAME, "message_id": sent.message_id})

                    # حفظ المراجع لو نُشر شيء
                    if saved_refs and ad_id:
                        try:
                            save_ad_message_refs(ad_id, saved_refs, append=True)
                        except Exception as e:
                            logging.warning(f"[ADS] save_ad_message_refs failed for ad_id={ad_id}: {e}")

                    # زيادة عدّاد النشر
                    if ad_id:
                        try:
                            increment_ad_posted(ad_id)
                        except Exception as e:
                            logging.warning(f"[ADS] increment_ad_posted failed for ad_id={ad_id}: {e}")

                except Exception as e:
                    # إعلان معيّن فشل؛ نكمل على الباقي
                    logging.error(f"[ADS] Failed to post ad id={ad.get('id')}: {e}", exc_info=True)

        else:
            logging.debug("[ADS] خارج نافذة النشر (10:00–22:00 بتوقيت سوريا).")

    except Exception as e:
        logging.error(f"[ADS] post_ads_task main loop error: {e}", exc_info=True)

    # جدولة الفحص التالي بعد ساعة
    threading.Timer(3600, post_ads_task, args=(bot,)).start()
