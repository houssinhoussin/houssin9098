from telebot import types
from config import ADMIN_MAIN_ID
from handlers import keyboards
try:
    from services.queue_service import add_pending_request
except Exception:
    def add_pending_request(*args, **kwargs):
        return None

import logging

# تخزين الطلبات التي بانتظار رد الأدمن
pending_support = {}

def register(bot, history):
    @bot.message_handler(func=lambda msg: msg.text == "🛠️ الدعم الفني")
    def request_support(msg):
        user_id = msg.from_user.id
        if user_id in pending_support:
            bot.send_message(msg.chat.id, "⏳ تم إرسال استفسارك بالفعل. الرجاء الانتظار حتى يتم الرد من الإدارة.")
            return

        name = msg.from_user.first_name
        text = (
            f"👋 مرحباً {name}!\n\n"
            "📌 هذا الخيار مخصص للتواصل مع الإدارة في الحالات الضرورية فقط.\n"
            "يرجى عدم استخدامه إلا إذا كنت بحاجة فعلية للمساعدة.\n\n"
            "هل ترغب فعلاً بالتواصل مع الإدارة؟"
        )

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("✅ تأكيد التواصل", callback_data="support_confirm"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="support_cancel")
        )

        history.setdefault(user_id, []).append("support_menu")
        bot.send_message(msg.chat.id, text, reply_markup=keyboard)

    @bot.callback_query_handler(func=lambda call: call.data in ["support_confirm", "support_cancel"])
    def handle_support_decision(call):
        user_id = call.from_user.id
        if call.data == "support_cancel":
            bot.edit_message_text("❌ تم إلغاء طلب التواصل مع الإدارة.", chat_id=call.message.chat.id, message_id=call.message.message_id)
            return

        pending_support[user_id] = "waiting_message"
        bot.edit_message_text("✉️ أرسل الآن استفسارك أو الشكوى برسالة واحدة فقط.", chat_id=call.message.chat.id, message_id=call.message.message_id)

    @bot.message_handler(func=lambda msg: pending_support.get(msg.from_user.id) == "waiting_message")
    def receive_support(msg):
        user_id = msg.from_user.id
        text = msg.text
        username = msg.from_user.username or "بدون اسم مستخدم"
        name = msg.from_user.first_name

        admin_msg = (
            f"📩 استفسار جديد:\n"
            f"👤 الاسم: {name} | @{username}\n"
            f"🆔 ID: `{user_id}`\n"
            f"💬 الرسالة:\n{text}"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✉️ الرد عليه", callback_data=f"reply_{user_id}"))

        add_pending_request(
            user_id=user_id,
            username=msg.from_user.username or "بدون اسم مستخدم",
            request_text=admin_msg
        )
        bot.send_message(ADMIN_MAIN_ID, admin_msg, parse_mode="Markdown", reply_markup=markup)
        bot.send_message(
            msg.chat.id,
            "✅ تم إرسال الاستفسار بنجاح. الرجاء انتظار رد الأدمن.",
            reply_markup=keyboards.support_menu()
        )
        pending_support[user_id] = "waiting_admin"

    @bot.callback_query_handler(func=lambda call: call.data.startswith("reply_"))
    def prompt_admin_reply(call):
        target_id = int(call.data.split("_")[1])
        pending_support[call.from_user.id] = f"replying_{target_id}"
        bot.send_message(call.message.chat.id, f"📝 أرسل الآن ردك للمستخدم `{target_id}`", parse_mode="Markdown")

    @bot.message_handler(func=lambda msg: str(pending_support.get(msg.from_user.id)).startswith("replying_"))
    def send_admin_reply(msg):
        target_id = int(pending_support[msg.from_user.id].split("_")[1])
        bot.send_message(target_id, f"📬 رد الأدمن:\n{msg.text}")
        bot.send_message(msg.chat.id, "✅ تم إرسال الرد للمستخدم.")
        pending_support.pop(msg.from_user.id, None)
        pending_support.pop(target_id, None)
