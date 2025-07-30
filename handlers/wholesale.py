from telebot import types
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from services.wallet_service import add_purchase, get_balance, has_sufficient_balance, deduct_balance
from services.queue_service import add_pending_request
import logging

user_wholesale_state = {}

WHOLESALE_DESCRIPTION = """
🛒 هذه الخدمة مخصصة لأصحاب المحلات والمراكز التجارية.
نوفّر لكم مجموعة من المواد الغذائية والمنظفات والمشروبات بأسعار الجملة.

🔻 المنتجات المقترحة تشمل:
🍫 الحلويات: شوكولا، بسكويت، سكاكر، علكة
🥤 مشروبات: مشروبات غازية، مياه معدنية، عصائر، مياه شرب معبئة
🍜 مواد غذائية: معكرونة، رز، شاي، زيت، سكر، معلبات
🧼 منظفات: مسحوق غسيل، صابون، شامبو، معقمات
🧴 العناية: كريمات، معجون أسنان، أدوات نظافة

✍️ يرجى الآن كتابة تفاصيل المنتجات التي ترغب بطلبها (نوع وكميات...)
"""

def register(bot, user_state):

    @bot.message_handler(func=lambda msg: msg.text == "📦 طلب احتياجات منزلية او تجارية")
    def start_wholesale(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id] = {"step": "products"}
        user_state[user_id] = "wholesale"
        bot.send_message(msg.chat.id, WHOLESALE_DESCRIPTION)

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "products")
    def get_product_details(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id]["products"] = msg.text.strip()
        user_wholesale_state[user_id]["step"] = "address"
        bot.send_message(msg.chat.id, "📍 الرجاء إدخال عنوان المتجر أو منطقة التوصيل:")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "address")
    def get_address(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id]["address"] = msg.text.strip()
        user_wholesale_state[user_id]["step"] = "phone"
        bot.send_message(msg.chat.id, "📞 الرجاء إدخال رقم الهاتف للتواصل:")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "phone")
    def get_phone(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id]["phone"] = msg.text.strip()
        user_wholesale_state[user_id]["step"] = "store"
        bot.send_message(msg.chat.id, "🏪 الرجاء إدخال اسم المتجر:")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "store")
    def get_store_name(msg):
        user_id = msg.from_user.id
        data = user_wholesale_state[user_id]
        data["store_name"] = msg.text.strip()

        text = f"""
🛍️ طلب جملة جديد من تاجر:

👤 المستخدم: {msg.from_user.first_name} | ID: {user_id}
📦 الطلب: {data['products']}
🏪 المتجر: {data['store_name']}
📍 العنوان: {data['address']}
📞 الهاتف: {data['phone']}
"""
        process_queue(bot)
        add_pending_request(
            user_id=user_id,
            username=msg.from_user.username or "بدون اسم مستخدم",
            request_text=text
        )
        bot.send_message(ADMIN_MAIN_ID, text)
        bot.send_message(msg.chat.id, "✅ تم إرسال طلبك للإدارة، سيتم التواصل معك قريبًا.")
        user_wholesale_state.pop(user_id, None)
