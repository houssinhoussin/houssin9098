# services/notification_service.py
# خدمة إرسال إشعارات للمستخدمين أو المسؤولين

import logging
from typing import Iterable, Optional

from config import (
    ADMIN_MAIN_ID,
    ADMIN_MAIN_USERNAME,
    TELEGRAM_PARSE_MODE,
    ADMINS,  # قد تكون قائمة تحتوي ADMIN_MAIN_ID ضمنيًا
)

DEFAULT_PARSE_MODE = TELEGRAM_PARSE_MODE or "HTML"

def _safe_send_message(bot, chat_id: int, text: str,
                       parse_mode: Optional[str] = DEFAULT_PARSE_MODE,
                       disable_web_page_preview: bool = True) -> bool:
    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return True
    except Exception as e:
        logging.warning(f"[NOTIFY] فشل إرسال رسالة إلى {chat_id}: {e}", exc_info=False)
        return False

def _safe_send_photo(bot, chat_id: int, file_id_or_url: str, caption: str = "",
                     parse_mode: Optional[str] = DEFAULT_PARSE_MODE) -> bool:
    try:
        bot.send_photo(
            chat_id,
            file_id_or_url,
            caption=caption,
            parse_mode=parse_mode,
        )
        return True
    except Exception as e:
        logging.warning(f"[NOTIFY] فشل إرسال صورة إلى {chat_id}: {e}", exc_info=False)
        return False

# ------------------------------------------------------
# الواجهات المتوافقة مع كودك الأصلي
# ------------------------------------------------------
def notify_admin(bot, text: str):
    """
    يرسل إشعارًا إلى الأدمن الرئيسي فقط (يحافظ على الواجهة الأصلية).
    """
    prefix = f"📣 إشعار من البوت ({ADMIN_MAIN_USERNAME}):\n" if ADMIN_MAIN_USERNAME else "📣 إشعار من البوت:\n"
    if not _safe_send_message(bot, ADMIN_MAIN_ID, prefix + text):
        logging.error(f"[NOTIFY] لم يتمكّن من إرسال إشعار للأدمن الرئيسي {ADMIN_MAIN_ID}")

def notify_user(bot, user_id: int, text: str):
    """
    يرسل رسالة مباشرة إلى مستخدم معيّن (يحافظ على الواجهة الأصلية).
    """
    if not _safe_send_message(bot, user_id, text):
        logging.error(f"[NOTIFY] لم يتمكّن من إرسال رسالة للمستخدم {user_id}")

# ------------------------------------------------------
# ميزات اختيارية إضافية (لا تؤثر على الكود القائم)
# ------------------------------------------------------
def notify_admins(bot, text: str, include_main: bool = True, admins: Optional[Iterable[int]] = None) -> int:
    """
    يرسل نفس الإشعار إلى جميع المدراء في ADMINS.
    يعيد عدد الرسائل المُرسلة بنجاح.
    """
    sent = 0
    target_admins = list(admins) if admins is not None else (ADMINS or [])
    if include_main and ADMIN_MAIN_ID not in target_admins:
        target_admins = [ADMIN_MAIN_ID] + list(target_admins)

    prefix = f"📣 إشعار إداري:\n"
    for admin_id in target_admins:
        if _safe_send_message(bot, admin_id, prefix + text):
            sent += 1
    return sent

def notify_admin_photo(bot, file_id_or_url: str, caption: str = "") -> bool:
    """
    يرسل صورة إلى الأدمن الرئيسي (مثالي لصور الإثبات/التحويل).
    """
    full_caption = caption or "📷"
    return _safe_send_photo(bot, ADMIN_MAIN_ID, file_id_or_url, full_caption)

def notify_user_photo(bot, user_id: int, file_id_or_url: str, caption: str = "") -> bool:
    """
    يرسل صورة إلى مستخدم معيّن.
    """
    return _safe_send_photo(bot, user_id, file_id_or_url, caption)
