# -*- coding: utf-8 -*-
# services/ui_guards.py — حارس تأكيد موحّد: يحذف الكيبورد ويفعّل Debounce

try:
    from services.telegram_safety import remove_inline_keyboard
except Exception:
    from telegram_safety import remove_inline_keyboard

try:
    from services.anti_spam import too_soon
except Exception:
    from anti_spam import too_soon


def confirm_guard(bot, call, key: str, debounce_seconds: int = 2) -> bool:
    """
    استخدام قياسي في كل دوال التأكيد:
    - يحذف الكيبورد بأمان (بدون حذف الرسالة ولا تعديل النص).
    - يعمل Debounce لمنع الدبل-كليك.
    يرجّع True لو لازم نخرج من الدالة (تم الاستلام).
    """
    # امسح الكيبورد فقط
    remove_inline_keyboard(bot, call.message)

    # امنع الدبل-كليك
    if too_soon(call.from_user.id, key, seconds=debounce_seconds):
        try:
            bot.answer_callback_query(call.id, "⏱️ تم استلام طلبك..")
        except Exception:
            pass
        return True

    return False
