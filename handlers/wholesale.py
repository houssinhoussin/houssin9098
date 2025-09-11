# -*- coding: utf-8 -*-
# handlers/wholesale.py
from handlers.start import _reset_user_flows
_reset_user_flows(m.from_user.id)

from telebot import types
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from services.queue_service import add_pending_request, process_queue
import logging
import re

# حرس نقر سريع + تنظيف كيبورد (لو استخدمنا Inline)
try:
    from services.anti_spam import too_soon
except Exception:
    def too_soon(*a, **k): return False

try:
    from services.telegram_safety import remove_inline_keyboard
except Exception:
    def remove_inline_keyboard(*a, **k): pass

BAND = "━━━━━━━━━━━━━━━━"

user_wholesale_state: dict[int, dict] = {}

WHOLESALE_DESCRIPTION = (
    "🛒 <b>خدمة الطلبات بالجملة</b>\n\n"
    "الخدمة دي مخصوص لأصحاب المحلات والمراكز التجارية:\n"
    "• غذائية: رز، شاي، زيت، سكر، معلبات\n"
    "• مشروبات: غازية، مياه، عصائر\n"
    "• حلويات: شوكولا، بسكويت، سكاكر\n"
    "• منظفات وعناية: مسحوق، صابون، شامبو…\n\n"
    "✍️ اكتب دلوقتي تفاصيل المطلوب (الأنواع + الكميات).\n"
    "اكتب /cancel في أي وقت للإلغاء."
)

_phone_re = re.compile(r"[+\d]+")

def _name(u):
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صاحبنا"

def _norm_phone(txt: str) -> str:
    if not txt:
        return ""
    clean = txt.replace(" ", "").replace("-", "").replace("_", "")
    parts = _phone_re.findall(clean)
    return "".join(parts)

def _ok_send_msg(name: str) -> str:
    return (
        f"{BAND}\n"
        f"✅ تمام يا {name}! طلبك اتبعت للإدارة.\n"
        f"📞 هنتواصل معاك قريب جدًا للتأكيد والتفاصيل.\n"
        f"{BAND}"
    )

def _summary_card(uid: int) -> str:
    d = user_wholesale_state.get(uid, {})
    return (
        f"{BAND}\n"
        "🛍️ <b>مراجعة الطلب</b>\n\n"
        f"📦 <b>المطلوب:</b> {d.get('products','—')}\n"
        f"📍 <b>العنوان:</b> {d.get('address','—')}\n"
        f"📞 <b>الهاتف:</b> {d.get('phone','—')}\n"
        f"🏪 <b>اسم المتجر:</b> {d.get('store_name','—')}\n"
        f"{BAND}\n"
        "لو كل حاجة تمام اضغط «تأكيد الإرسال».\n"
        "اكتب /cancel للإلغاء."
    )

def _confirm_kb():
    mk = types.InlineKeyboardMarkup()
    mk.add(
        types.InlineKeyboardButton("✅ تأكيد الإرسال", callback_data="ws_confirm"),
        types.InlineKeyboardButton("📝 تعديل", callback_data="ws_edit"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="ws_cancel"),
    )
    return mk

def register(bot, user_state):
    # إلغاء الجلسة
    @bot.message_handler(commands=["cancel"])
    def ws_cancel_cmd(msg):
        uid = msg.from_user.id
        if uid in user_wholesale_state:
            user_wholesale_state.pop(uid, None)
            bot.reply_to(msg, "✅ اتلغت العملية. تقدر تبدأ من جديد وقت ما تحب.")
        else:
            bot.reply_to(msg, "لا يوجد إجراء جارٍ حاليًا.")

    @bot.message_handler(func=lambda msg: msg.text == "📦 طلب احتياجات منزلية او تجارية")
    def start_wholesale(msg):
        uid = msg.from_user.id
        register_user_if_not_exist(uid, msg.from_user.full_name)
        user_wholesale_state[uid] = {"step": "products"}
        try:
            user_state[uid] = "wholesale"
        except Exception:
            pass
        bot.send_message(msg.chat.id, WHOLESALE_DESCRIPTION, parse_mode="HTML")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "products")
    def get_product_details(msg):
        uid = msg.from_user.id
        user_wholesale_state[uid]["products"] = (msg.text or "").strip()
        user_wholesale_state[uid]["step"] = "address"
        bot.send_message(msg.chat.id, "📍 اكتب عنوان المتجر أو منطقة التوصيل:\n/cancel للإلغاء")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "address")
    def get_address(msg):
        uid = msg.from_user.id
        user_wholesale_state[uid]["address"] = (msg.text or "").strip()
        user_wholesale_state[uid]["step"] = "phone"
        bot.send_message(msg.chat.id, "📞 اكتب رقم الموبايل للتواصل:\n/cancel للإلغاء")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "phone")
    def get_phone(msg):
        uid = msg.from_user.id
        phone = _norm_phone(msg.text or "")
        if len(phone) < 6:
            return bot.reply_to(msg, "⚠️ الرقم مش واضح. اكتب رقم صالح.\n/cancel للإلغاء")
        user_wholesale_state[uid]["phone"] = phone
        user_wholesale_state[uid]["step"] = "store"
        bot.send_message(msg.chat.id, "🏪 اكتب اسم المتجر:\n/cancel للإلغاء")

    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "store")
    def get_store_name(msg):
        uid = msg.from_user.id
        user_wholesale_state[uid]["store_name"] = (msg.text or "").strip()
        user_wholesale_state[uid]["step"] = "confirm"
        bot.send_message(
            msg.chat.id,
            _summary_card(uid),
            parse_mode="HTML",
            reply_markup=_confirm_kb()
        )

    # أزرار التأكيد/التعديل/الإلغاء
    @bot.callback_query_handler(func=lambda c: c.data in {"ws_confirm", "ws_cancel", "ws_edit"})
    def ws_actions(c: types.CallbackQuery):
        uid = c.from_user.id
        st = user_wholesale_state.get(uid)
        if not st:
            return bot.answer_callback_query(c.id, "انتهت الجلسة. ابدأ من جديد لو سمحت.", show_alert=True)

        # تنظيف الكيبورد (ومنـع الدبل-كليك)
        try:
            remove_inline_keyboard(bot, c.message)
        except Exception:
            pass
        if too_soon(uid, "ws_actions", seconds=2):
            return bot.answer_callback_query(c.id, "⏱️ تم الاستلام..")

        if c.data == "ws_cancel":
            user_wholesale_state.pop(uid, None)
            bot.answer_callback_query(c.id, "تم الإلغاء")
            return bot.send_message(uid, "✅ اتلغت العملية. نورتنا 🙏")

        if c.data == "ws_edit":
            # ارجَع لأول نقطة (المنتجات)
            st["step"] = "products"
            return bot.send_message(uid, "✏️ عدّل تفاصيل المطلوب (الأنواع + الكميات):")

        # تأكيد
        if st.get("step") != "confirm":
            return bot.answer_callback_query(c.id, "الطلب غير جاهز للتأكيد.", show_alert=True)

        name = _name(c.from_user)
        text = (
            "🛍️ <b>طلب جملة جديد</b>\n\n"
            f"👤 المستخدم: <code>{name}</code> | ID: <code>{uid}</code>\n"
            f"📦 المطلوب: {st.get('products')}\n"
            f"🏪 المتجر: {st.get('store_name')}\n"
            f"📍 العنوان: {st.get('address')}\n"
            f"📞 الهاتف: {st.get('phone')}\n"
        )

        # أرسل للطابور برسالة موحّدة + payload مفيد
        add_pending_request(
            user_id=uid,
            username=c.from_user.username or "—",
            request_text=text,
            payload={
                "type": "wholesale",
                "products": st.get("products"),
                "store_name": st.get("store_name"),
                "address": st.get("address"),
                "phone": st.get("phone"),
            }
        )
        process_queue(bot)

        # نسخة مباشرة للأدمن (اختياري)
        try:
            bot.send_message(ADMIN_MAIN_ID, text, parse_mode="HTML")
        except Exception:
            logging.exception("[WHOLESALE] فشل إرسال نسخة للأدمن")

        # رسالة للعميل
        bot.send_message(uid, _ok_send_msg(_name(c.from_user)), parse_mode="HTML")
        user_wholesale_state.pop(uid, None)
        bot.answer_callback_query(c.id, "تم الإرسال ✅")
