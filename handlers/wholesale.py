# handlers/wholesale.py
from telebot import types
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from services.wallet_service import add_purchase, get_balance, has_sufficient_balance, deduct_balance
from services.queue_service import add_pending_request, process_queue
import logging

# فاصل موحّد
BAND = "━━━━━━━━━━━━━━━━"

user_wholesale_state = {}

WHOLESALE_DESCRIPTION = (
    "🛒 الخدمة دي مخصوص لأصحاب المحلات والمراكز التجارية.\n"
    "بنوفّرلكم مواد غذائية ومنظفات ومشروبات بأسعار الجملة.\n\n"
    "🔻 أمثلة المنتجات:\n"
    "🍫 الحلويات: شوكولا، بسكويت، سكاكر، علكة\n"
    "🥤 مشروبات: غازية، مياه معدنية، عصائر\n"
    "🍜 غذائية: معكرونة، رز، شاي، زيت، سكر، معلبات\n"
    "🧼 منظفات: مسحوق غسيل، صابون، شامبو، معقمات\n"
    "🧴 العناية: كريمات، معجون أسنان، أدوات نظافة\n\n"
    "✍️ اكتب دلوقتي تفاصيل المطلوب (أنواع + كميات)..."
)

def _name(u):
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صاحبنا"

def _ok_send_msg(name: str) -> str:
    return (
        f"{BAND}\n"
        f"✅ تمام يا {name}! طلبك اتبعت للإدارة على طول.\n"
        f"📞 هنتواصل معاك قريب جدًا عشان التأكيد والتفاصيل.\n"
        f"{BAND}"
    )

def register(bot, user_state):

    @bot.message_handler(func=lambda msg: msg.text == "📦 طلب احتياجات منزلية او تجارية")
    def start_wholesale(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)
        user_wholesale_state[user_id] = {"step": "products"}
        user_state[user_id] = "wholesale"
        bot.send_message(msg.chat.id, WHOLESALE_DESCRIPTION)

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "products")
    def get_product_details(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id]["products"] = msg.text.strip()
        user_wholesale_state[user_id]["step"] = "address"
        bot.send_message(msg.chat.id, "📍 اكتب عنوان المتجر أو منطقة التوصيل:")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "address")
    def get_address(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id]["address"] = msg.text.strip()
        user_wholesale_state[user_id]["step"] = "phone"
        bot.send_message(msg.chat.id, "📞 اكتب رقم الموبايل للتواصل:")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "phone")
    def get_phone(msg):
        user_id = msg.from_user.id
        user_wholesale_state[user_id]["phone"] = msg.text.strip()
        user_wholesale_state[user_id]["step"] = "store"
        bot.send_message(msg.chat.id, "🏪 اكتب اسم المتجر:")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "store")
    def get_store_name(msg):
        user_id = msg.from_user.id
        data = user_wholesale_state[user_id]
        data["store_name"] = msg.text.strip()

        text = (
            "🛍️ طلب جملة جديد من تاجر:\n\n"
            f"👤 المستخدم: {msg.from_user.first_name} | ID: {user_id}\n"
            f"📦 الطلب: {data['products']}\n"
            f"🏪 المتجر: {data['store_name']}\n"
            f"📍 العنوان: {data['address']}\n"
            f"📞 الهاتف: {data['phone']}\n"
        )

        add_pending_request(
            user_id=user_id,
            username=msg.from_user.username or "بدون اسم مستخدم",
            request_text=text
        )
        process_queue(bot)

        # إشعار الأدمن (لو حابب يبقى عنده نسخة مباشرة)
        try:
            bot.send_message(ADMIN_MAIN_ID, text)
        except Exception:
            logging.exception("[WHOLESALE] فشل إرسال نسخة للأدمن")

        # رسالة موحّدة للعميل بأسلوب تسويقي
        bot.send_message(msg.chat.id, _ok_send_msg(_name(msg.from_user)))
        user_wholesale_state.pop(user_id, None)
