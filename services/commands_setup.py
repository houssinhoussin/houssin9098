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
