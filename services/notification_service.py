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
