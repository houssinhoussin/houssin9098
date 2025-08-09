# handlers/media_services.py
from telebot import types
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from services.wallet_service import add_purchase, get_balance, has_sufficient_balance,deduct_balance
from handlers.keyboards import media_services_menu
from services.queue_service import add_pending_request
import logging

# حالة المستخدم داخل سير عمل خدمات الإعلام
user_media_state = {}
USD_RATE = 11000  # سعر الصرف ليرة/دولار
MEDIA_PRODUCTS = {
    "🖼️ تصميم لوغو احترافي": 300,
    "📱 إدارة ونشر يومي": 300,
    "📢 إطلاق حملة إعلانية": 300,
    "🧾 باقة متكاملة شهرية": 300,
    "✏️ طلب مخصص": 0,
}

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def register(bot, user_state):
    @bot.message_handler(func=lambda msg: msg.text == "🖼️ خدمات إعلانية وتصميم")
    def open_media_menu(msg):
        user_id = msg.from_user.id
        # تعيين حالة القائمة الرئيسية إلى خدمات إعلامية
        user_state[user_id] = "media_services"
        # بداية سير العمل لاختيار الخدمة
        user_media_state[user_id] = {"step": "choose_service"}
        bot.send_message(
            msg.chat.id,
            "🎨 اختر الخدمة التي تريدها:",
            reply_markup=media_services_menu()
        )

    @bot.message_handler(func=lambda msg: user_media_state.get(msg.from_user.id, {}).get("step") == "choose_service" and msg.text in MEDIA_PRODUCTS)
    def handle_selected_service(msg):
        user_id = msg.from_user.id
        service = msg.text
        price_usd = MEDIA_PRODUCTS[service]
        if price_usd > 0:
            price_syp = price_usd * USD_RATE
            user_media_state[user_id] = {
                "step": "confirm_service",
                "service": service,
                "price_usd": price_usd,
                "price_syp": price_syp
            }
            text = (
                f"💵 سعر الخدمة «{service}» هو {price_syp:,} ل.س\n"
                f"(معدل التحويل {USD_RATE} ل.س/دولار)\n"
                "هل تريد المتابعة؟"
            )
            kb = make_inline_buttons(
                ("✅ موافق", "media_confirm"),
                ("❌ إلغاء", "media_cancel")
            )
            bot.send_message(msg.chat.id, text, reply_markup=kb)
        else:
            # طلب مخصص
            user_media_state[user_id] = {"step": "custom_details", "service": service}
            bot.send_message(msg.chat.id, "📝 اكتب تفاصيل طلبك المخصص:")

    @bot.message_handler(func=lambda msg: user_media_state.get(msg.from_user.id, {}).get("step") == "custom_details")
    def handle_custom_details(msg):
        user_id = msg.from_user.id
        state = user_media_state[user_id]
        state["details"] = msg.text
        state["step"] = "custom_price"
        bot.send_message(msg.chat.id, "💵 اكتب السعر بالدولار للخدمة المخصصة:")

    @bot.message_handler(func=lambda msg: user_media_state.get(msg.from_user.id, {}).get("step") == "custom_price")
    def handle_custom_price(msg):
        user_id = msg.from_user.id
        state = user_media_state[user_id]
        try:
            price_usd = float(msg.text)
        except ValueError:
            return bot.send_message(msg.chat.id, "⚠️ الرجاء إدخال سعر صحيح بالأرقام.")
        price_syp = int(price_usd * USD_RATE)
        state.update({
            "step": "confirm_service",
            "price_usd": price_usd,
            "price_syp": price_syp
        })
        details = state.get("details", "")
        kb = make_inline_buttons(
            ("✅ موافق", "media_confirm"),
            ("❌ إلغاء", "media_cancel")
        )
        bot.send_message(
            msg.chat.id,
            f"📝 تفاصيل: {details}\n💵 السعر: {price_syp:,} ل.س\n"
            "هل تريد المتابعة؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "media_cancel")
    def cancel_media(call):
        user_id = call.from_user.id
        bot.edit_message_text(
            "❌ تم إلغاء العملية.",
            call.message.chat.id,
            call.message.message_id
        )
        user_media_state.pop(user_id, None)

    @bot.callback_query_handler(func=lambda call: call.data == "media_confirm")
    def confirm_media(call):
        user_id = call.from_user.id
        state = user_media_state.pop(user_id, {})
        service = state.get("service")
        price_syp = state.get("price_syp", 0)
        details = state.get("details", "")

        # التحقق من الرصيد (إذا السعر > 0)
        if price_syp > 0 and not has_sufficient_balance(user_id, price_syp):
            bot.edit_message_text(
                "❌ لا يوجد رصيد كافٍ في محفظتك لإتمام هذه الخدمة.",
                call.message.chat.id,
                call.message.message_id
            )
            return

        # خصم الرصيد إن وُجد سعر
        if price_syp > 0:
            deduct_balance(user_id, price_syp)

        # بناء رسالة للإدارة
        admin_msg = (
            f"📢 طلب خدمة إعلانية/تصميم جديدة:\n"
            f"👤 المستخدم: {user_id}\n"
            f"🎨 الخدمة: {service}\n"
        )
        if details:
            admin_msg += f"📝 تفاصيل: {details}\n"
        if price_syp > 0:
            admin_msg += f"💵 السعر: {price_syp:,} ل.س"

        # تأكيد للمستخدم
        bot.edit_message_text(
            "✅ تم إرسال طلبك بنجاح، بانتظار المعالجة من الإدارة.",
            call.message.chat.id,
            call.message.message_id
        )
        # إرسال للإدارة
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg
        )
        bot.send_message(ADMIN_MAIN_ID, admin_msg)

        # إعادة المستخدم لقائمة المنتجات
        user_state[user_id] = "products_menu"
