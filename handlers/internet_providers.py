# -*- coding: utf-8 -*-
# handlers/internet_providers.py  (Inline + موافقة أدمن قبل الخصم + رد أدمن للمستخدم)

import logging
import re

from telebot import types

from config import ADMIN_MAIN_ID
from database.models.product import Product
from services.wallet_service import (
    register_user_if_not_exist,
    add_purchase,
    get_balance,
    has_sufficient_balance,
    deduct_balance,
)
from services.queue_service import (
    add_pending_request,
    process_queue,
    delete_pending_request,
)
from database.db import get_table  # لمنع الطلبات المتزامنة
# =====================================
#       ثوابت
# =====================================
INTERNET_PROVIDERS = [
    "تراسل", "أم تي أن", "سيرياتيل", "آية", "سوا", "رن نت", "سما نت", "أمنية",
    "ناس", "هايبر نت", "MTS", "يارا", "دنيا", "آينت"
]

INTERNET_SPEEDS = [
    {"label": "1 ميغا",  "price": 19500},
    {"label": "2 ميغا",  "price": 25000},
    {"label": "4 ميغا",  "price": 39000},
    {"label": "8 ميغا",  "price": 65000},
    {"label": "16 ميغا", "price": 84000},
]

COMMISSION_PER_5000 = 600

# حالة المستخدم (نوع الطلب والخطوات)
user_net_state = {}  # { user_id: { step, provider?, speed?, price?, phone? } }

# =====================================
#   وظائف مساعدة
# =====================================
_PHONE_RE = re.compile(r"[+\d]+")

def _normalize_phone(txt: str) -> str:
    if not txt:
        return ""
    clean = txt.replace(" ", "").replace("-", "").replace("_", "")
    m = _PHONE_RE.findall(clean)
    return ''.join(m)

def calculate_commission(amount: int) -> int:
    if amount <= 0:
        return 0
    blocks = (amount + 5000 - 1) // 5000
    return blocks * COMMISSION_PER_5000

# =====================================
#   مفاتيح callback
# =====================================
CB_PROV_PREFIX   = "iprov"      # اختيار مزوّد
CB_SPEED_PREFIX = "ispeed"     # اختيار سرعة
CB_BACK_PROV     = "iback_prov"   # رجوع لقائمة المزودين
CB_BACK_SPEED   = "iback_speed"  # رجوع لقائمة السرعات
CB_CONFIRM       = "iconfirm"     # تأكيد (إرسال لطابور الأدمن)
CB_CANCEL        = "icancel"      # إلغاء من المستخدم

# Inline keyboards
def _provider_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [
        types.InlineKeyboardButton(f"🌐 {name}", callback_data=f"{CB_PROV_PREFIX}:{name}")
        for name in INTERNET_PROVIDERS
    ]
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL))
    return kb

def _speeds_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [
        types.InlineKeyboardButton(
            text=f"{speed['label']} - {speed['price']:,} ل.س",
            callback_data=f"{CB_SPEED_PREFIX}:{idx}"
        )
        for idx, speed in enumerate(INTERNET_SPEEDS)
    ]
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data=CB_BACK_PROV))
    return kb

def _confirm_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تأكيد", callback_data=CB_CONFIRM),
        types.InlineKeyboardButton("⬅️ تعديل", callback_data=CB_BACK_SPEED),
        types.InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL),
    )
    return kb

# =====================================
#   بدء القوائم وتسجيل المعالجات
# =====================================
def register(bot):
    """تسجيل معالجات مزودي الإنترنت."""
    # فتح القائمة الرئيسية
    @bot.message_handler(func=lambda msg: msg.text == "🌐 دفع مزودات الإنترنت ADSL")
    def open_net_menu(msg):
        start_internet_provider_menu(bot, msg)

    # اختيار مزود
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_PROV_PREFIX}:"))
    def cb_choose_provider(call):
        user_id = call.from_user.id
        provider = call.data.split(":", 1)[1]
        if provider not in INTERNET_PROVIDERS:
            return bot.answer_callback_query(call.id, "خيار غير صالح.", show_alert=True)
        user_net_state[user_id] = {"step": "choose_speed", "provider": provider}
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="⚡ اختر السرعة المطلوبة:\n💸 العمولة لكل 5000 ل.س = 600 ل.س",
            reply_markup=_speeds_inline_kb()
        )

    # رجوع لقائمة المزودين
    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_PROV)
    def cb_back_to_prov(call):
        user_id = call.from_user.id
        user_net_state[user_id] = {"step": "choose_provider"}
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="⚠️ اختر أحد مزودات الإنترنت:\n💸 العمولة لكل 5000 ل.س = 600 ل.س",
            reply_markup=_provider_inline_kb()
        )

    # اختيار سرعة
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SPEED_PREFIX}:"))
    def cb_choose_speed(call):
        user_id = call.from_user.id
        try:
            idx = int(call.data.split(":", 1)[1])
            speed = INTERNET_SPEEDS[idx]
        except (ValueError, IndexError):
            return bot.answer_callback_query(call.id, "خيار غير صالح.", show_alert=True)

        st = user_net_state.setdefault(user_id, {})
        st.update({
            "step": "enter_phone",
            "provider": st.get("provider"),
            "speed": speed["label"],
            "price": speed["price"]
        })

        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id=call.message.chat.id,
            text="📱 أرسل رقم الهاتف / الحساب المطلوب شحنه (مع رمز المحافظة، مثال: 011XXXXXXX).\nأرسل /cancel للإلغاء."
        )

    # رجوع لشاشة السرعات
    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_SPEED)
    def cb_back_to_speed(call):
        user_id = call.from_user.id
        st = user_net_state.get(user_id, {})
        if "provider" not in st:
            return cb_back_to_prov(call)
        st["step"] = "choose_speed"
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="⚡ اختر السرعة المطلوبة:\n💸 العمولة لكل 5000 ل.س = 600 ل.س",
            reply_markup=_speeds_inline_kb()
        )

    # إلغاء من المستخدم
    @bot.callback_query_handler(func=lambda c: c.data == CB_CANCEL)
    def cb_cancel(call):
        user_net_state.pop(call.from_user.id, None)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="تم الإلغاء. أرسل /start للعودة إلى القائمة الرئيسية."
        )

    # إدخال رقم الهاتف
    @bot.message_handler(func=lambda m: user_net_state.get(m.from_user.id, {}).get("step") == "enter_phone")
    def handle_phone_entry(msg):
        user_id = msg.from_user.id
        phone = _normalize_phone(msg.text)
        if not phone or len(phone) < 5:
            return bot.reply_to(msg, "⚠️ رقم غير صالح، أعد الإرسال.")

        st = user_net_state[user_id]
        st["phone"] = phone
        st["step"] = "confirm"

        price = st["price"]
        comm  = calculate_commission(price)
        total = price + comm

        summary = (
            "📦 *تفاصيل الطلب*\n"
            f"مزود: {st['provider']}\n"
            f"سرعة: {st['speed']}\n"
            f"السعر: {price:,} ل.س\n"
            f"العمولة: {comm:,} ل.س\n"
            f"الإجمالي: {total:,} ل.س\n\n"
            f"رقم: `{phone}`\n\n"
            "اضغط لإرسال الطلب إلى الأدمن (لن يتم خصم أي مبلغ الآن)."
        )
        bot.send_message(
            msg.chat.id,
            summary,
            parse_mode="Markdown",
            reply_markup=_confirm_inline_kb()
        )

    # إرسال الطلب إلى طابور الأدمن مع حجز المبلغ
    @bot.callback_query_handler(func=lambda c: c.data == CB_CONFIRM)
    def cb_confirm(call):
        user_id = call.from_user.id
        st = user_net_state.get(user_id)
        if not st or st.get("step") != "confirm":
            return bot.answer_callback_query(call.id, "انتهت صلاحية هذا الطلب.", show_alert=True)

        # منع الطلبات المتزامنة
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if getattr(existing, 'data', None):
            return bot.answer_callback_query(call.id, "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.", show_alert=True)

        price = st["price"]
        comm  = calculate_commission(price)
        total = price + comm

        balance = get_balance(user_id)
        if balance < total:
            missing = total - balance
            return bot.answer_callback_query(
                call.id,
                f"❌ رصيدك الحالي: {balance:,} ل.س\nالناقص: {missing:,} ل.س\nيرجى شحن المحفظة أولاً.",
                show_alert=True
            )

        # حجز الرصيد
        deduct_balance(user_id, total)

        adm_txt = (
            "📥 *طلب جديد (إنترنت)*\n"
            f"المستخدم: {user_id}\n"
            f"رصيد المستخدم: {balance:,} ل.س\n"
            f"مزود: {st['provider']}\n"
            f"سرعة: {st['speed']}\n"
            f"رقم: `{st['phone']}`\n"
            f"المبلغ: {price:,} + عمولة {comm:,} = {total:,} ل.س"
        )
        print(f"[DEBUG] Adding pending request with reserved amount: {total}")
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=adm_txt,
            payload={
                "type": "internet",
                "provider": st["provider"],
                "speed": st["speed"],
                "phone": st["phone"],
                "price": price,
                "comm": comm,
                "total": total,
                "reserved": total,
            }
        )
        process_queue(bot)

        bot.answer_callback_query(call.id, "✅ تم إرسال طلبك للإدارة، بانتظار الموافقة.")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="📨 تم إرسال طلبك لمسؤول البوت. سيتم إشعارك بعد المراجعة."
        )

        st["step"] = "wait_admin"

def start_internet_provider_menu(bot, message):
    bot.send_message(
        message.chat.id,
        "⚠️ اختر أحد مزودات الإنترنت:\n💸 العمولة لكل 5000 ل.س = 600 ل.س",
        reply_markup=_provider_inline_kb()
    )
    user_net_state[message.from_user.id] = {"step": "choose_provider"}
