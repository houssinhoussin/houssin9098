# services/notification_service.py
# خدمة إرسال إشعارات للمستخدمين أو المسؤولين
from config import ADMIN_MAIN_ID, ADMIN_MAIN_USERNAME

def notify_admin(bot, text):
    try:
        bot.send_message(ADMIN_MAIN_ID, f"📣 إشعار من البوت ({ADMIN_MAIN_USERNAME}):\n{text}")
    except Exception as e:
        print(f"❌ فشل في إرسال إشعار للأدمن: {e}")

def notify_user(bot, user_id, text):
    try:
        bot.send_message(user_id, text)
    except Exception as e:
        print(f"❌ فشل في إرسال رسالة للمستخدم {user_id}: {e}")


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
