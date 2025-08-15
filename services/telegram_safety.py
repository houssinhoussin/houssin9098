# -*- coding: utf-8 -*-
# services/telegram_safety.py — قفل الواجهة بعد التأكيد + وظائف مساعدة

from telebot import types
import logging

def remove_inline_keyboard(bot, message):
    """يحذف/يعطّل الأزرار المضمنة للرسالة المُحددة."""
    try:
        bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=message.message_id, reply_markup=None)
    except Exception as e:
        logging.debug(f"remove_inline_keyboard: {e}")

def safe_finalize(bot, message, new_text=None, parse_mode=None):
    """تحرير نص الرسالة (اختياري) مع إزالة الأزرار. لو فشل، نرسل رسالة جديدة."""
    try:
        if new_text is not None:
            bot.edit_message_text(
                new_text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                parse_mode=parse_mode,
                reply_markup=None,
            )
        else:
            bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=None,
            )
        return True
    except Exception as e:
        logging.debug(f"safe_finalize: {e}")
        try:
            if new_text is not None:
                bot.send_message(message.chat.id, new_text, parse_mode=parse_mode)
        except Exception as ee:
            logging.debug(f"safe_finalize fallback: {ee}")
        return False

