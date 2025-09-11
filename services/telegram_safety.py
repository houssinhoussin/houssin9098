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



@bot.message_handler(commands=['cancel'])
def cancel_cmd(m):
    try:
        for dct in (globals().get('_msg_by_id_pending', {}),
                    globals().get('_disc_new_user_state', {}),
                    globals().get('_admin_manage_user_state', {}),
                    globals().get('_address_state', {}),
                    globals().get('_phone_state', {})):
            try:
                dct.pop(m.from_user.id, None)
            except Exception:
                pass
    except Exception:
        pass
    try:
        bot.reply_to(m, "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.")
    except Exception:
        bot.send_message(m.chat.id, "✅ تم الإلغاء.")
