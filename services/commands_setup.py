# services/commands_setup.py
from telebot import types

def setup_bot_commands(bot, admins: list[int]):
    # أوامر عامة لكل المستخدمين
    bot.set_my_commands([
        types.BotCommand('start', 'لبدء رحلتك في البوت'),
    ])
    # أوامر خاصة لكل أدمن على حدة (تظهر في قائمة Menu لديه)
    for aid in admins:
        try:
            bot.set_my_commands([
                types.BotCommand('start', 'لبدء رحلتك في البوت'),
                types.BotCommand('admin', 'للمشرفين فقط'),
            ], scope=types.BotCommandScopeChat(chat_id=aid))
        except Exception:
            # تجاهل أي خطأ في ضبط أوامر خاصّة لعدم تعطيل البوت
            pass


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
