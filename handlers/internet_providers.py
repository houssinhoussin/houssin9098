# -*- coding: utf-8 -*-
# handlers/internet_providers.py — قوائم ADSL مع حجز ذري + رسائل موحّدة

import logging
import re
from telebot import types

from services.wallet_service import (
    register_user_if_not_exist,
    get_balance,
    get_available_balance,   # ✅ المتاح = balance - held
    create_hold,             # ✅ إنشاء الحجز الذرّي
)
from services.queue_service import add_pending_request, process_queue
from services.telegram_safety import remove_inline_keyboard
from services.anti_spam import too_soon

# =====================================
#       إعدادات عامة / ثوابت
# =====================================
BAND = "━━━━━━━━━━━━━━━━"
COMMISSION_PER_5000 = 600

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

# حالة المستخدم (نوع الطلب والخطوات)
user_net_state = {}  # { user_id: { step, provider?, speed?, price?, phone? } }

# =====================================
#   أدوات مساعدة / تنسيق موحّد
# =====================================
_PHONE_RE = re.compile(r"[+\d]+")

def _name(bot, uid) -> str:
    try:
        ch = bot.get_chat(uid)
        nm = (getattr(ch, "first_name", None) or getattr(ch, "full_name", "") or "").strip()
        return nm or "صاحبنا"
    except Exception:
        return "صاحبنا"

def _normalize_phone(txt: str) -> str:
    if not txt:
        return ""
    clean = txt.replace(" ", "").replace("-", "").replace("_", "")
    m = _PHONE_RE.findall(clean)
    return "".join(m)

def _fmt_syp(n) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _commission(amount: int) -> int:
    if amount <= 0:
        return 0
    blocks = (amount + 5000 - 1) // 5000
    return blocks * COMMISSION_PER_5000

def _client_card(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def _admin_card(lines: list[str]) -> str:
    return "\n".join(lines)

# =====================================
#   مفاتيح callback
# =====================================
CB_PROV_PREFIX   = "iprov"         # اختيار مزوّد
CB_SPEED_PREFIX  = "ispeed"        # اختيار سرعة
CB_BACK_PROV     = "iback_prov"    # رجوع لقائمة المزودين
CB_BACK_SPEED    = "iback_speed"   # رجوع لقائمة السرعات
CB_CONFIRM       = "iconfirm"      # تأكيد الطلب
CB_CANCEL        = "icancel"       # إلغاء

# =====================================
#   لوحات أزرار Inline
# =====================================
def _provider_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(f"🌐 {name}", callback_data=f"{CB_PROV_PREFIX}:{name}") for name in INTERNET_PROVIDERS]
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL))
    return kb

def _speeds_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [
        types.InlineKeyboardButton(
            text=f"{speed['label']} • {_fmt_syp(speed['price'])}",
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
#   التسجيل
# =====================================
def register(bot):
    # فتح القائمة الرئيسية
    @bot.message_handler(func=lambda msg: msg.text == "🌐 دفع مزودات الإنترنت ADSL")
    def open_net_menu(msg):
        register_user_if_not_exist(msg.from_user.id, msg.from_user.full_name)
        start_internet_provider_menu(bot, msg)

    # اختيار مزوّد
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_PROV_PREFIX}:"))
    def cb_choose_provider(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        provider = call.data.split(":", 1)[1]
        if provider not in INTERNET_PROVIDERS:
            return bot.answer_callback_query(call.id, "❌ خيار غير صالح.", show_alert=True)

        user_net_state[uid] = {"step": "choose_speed", "provider": provider}
        txt = _client_card(
            f"⚡ يا {nm}، اختار السرعة المطلوبة",
            [f"💸 العمولة لكل 5000 ل.س: {_fmt_syp(COMMISSION_PER_5000)}"]
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=txt,
            reply_markup=_speeds_inline_kb()
        )

    # رجوع لقائمة المزوّدين
    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_PROV)
    def cb_back_to_prov(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        user_net_state[uid] = {"step": "choose_provider"}
        txt = _client_card(
            f"⚠️ يا {nm}، اختار مزوّد الإنترنت",
            [f"💸 العمولة لكل 5000 ل.س: {_fmt_syp(COMMISSION_PER_5000)}"]
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=txt,
            reply_markup=_provider_inline_kb()
        )

    # اختيار سرعة
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SPEED_PREFIX}:"))
    def cb_choose_speed(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        try:
            idx = int(call.data.split(":", 1)[1])
            speed = INTERNET_SPEEDS[idx]
        except (ValueError, IndexError):
            return bot.answer_callback_query(call.id, "❌ خيار غير صالح.", show_alert=True)

        st = user_net_state.setdefault(uid, {})
        st.update({
            "step": "enter_phone",
            "provider": st.get("provider"),
            "speed": speed["label"],
            "price": speed["price"]
        })
        bot.answer_callback_query(call.id)
        txt = _client_card(
            f"📱 يا {nm}، ابعت رقم الهاتف/الحساب المطلوب شحنه",
            ["يُفضّل مع رمز المحافظة (مثال: 011XXXXXXX)", "اكتب /cancel للإلغاء."]
        )
        bot.send_message(call.message.chat.id, txt)

    # رجوع لشاشة السرعات
    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_SPEED)
    def cb_back_to_speed(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        st = user_net_state.get(uid, {})
        if "provider" not in st:
            return cb_back_to_prov(call)
        st["step"] = "choose_speed"
        txt = _client_card(
            f"⚡ يا {nm}، اختار السرعة المطلوبة",
            [f"💸 العمولة لكل 5000 ل.س: {_fmt_syp(COMMISSION_PER_5000)}"]
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=txt,
            reply_markup=_speeds_inline_kb()
        )

    # إلغاء من المستخدم
    @bot.callback_query_handler(func=lambda c: c.data == CB_CANCEL)
    def cb_cancel(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        user_net_state.pop(uid, None)
        txt = _client_card("✅ اتلغت العملية", [f"يا {nm}، ابعت /start عشان ترجع للقائمة الرئيسية."])
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=txt
        )

    # إدخال رقم الهاتف
    @bot.message_handler(func=lambda m: user_net_state.get(m.from_user.id, {}).get("step") == "enter_phone")
    def handle_phone_entry(msg):
        uid = msg.from_user.id
        nm = _name(bot, uid)
        phone = _normalize_phone(msg.text)
        if not phone or len(phone) < 5:
            return bot.reply_to(msg, _client_card("⚠️ الرقم مش واضح", [f"يا {nm}، ابعته تاني بشكل صحيح."]))

        st = user_net_state[uid]
        st["phone"] = phone
        st["step"] = "confirm"

        price = st["price"]
        comm  = _commission(price)
        total = price + comm

        lines = [
            f"🌐 المزوّد: {st['provider']}",
            f"⚡ السرعة: {st['speed']}",
            f"💰 السعر: {_fmt_syp(price)}",
            f"🧾 العمولة: {_fmt_syp(comm)}",
            f"✅ الإجمالي: {_fmt_syp(total)}",
            "",
            f"📞 الرقم/الحساب: {phone}",
            "",
            "لو تمام، اضغط (✅ تأكيد) عشان نبعت الطلب للإدارة."
        ]
        bot.send_message(msg.chat.id, _client_card(f"📦 تفاصيل الطلب — يا {nm}", lines), reply_markup=_confirm_inline_kb())

    # تأكيد وإرسال إلى طابور الأدمن + إنشاء HOLD
    @bot.callback_query_handler(func=lambda c: c.data == CB_CONFIRM)
    def cb_confirm(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        st = user_net_state.get(uid)
        if not st or st.get("step") != "confirm":
            return bot.answer_callback_query(call.id, "انتهت صلاحية هذا الطلب.", show_alert=True)

        price = st["price"]
        comm  = _commission(price)
        total = price + comm

        # ✅ نعتمد على الرصيد المتاح فقط (balance − held)
        available = get_available_balance(uid)
        if available < total:
            missing = total - available
            msg_txt = _client_card(
                "❌ رصيدك مش مكفّي",
                [f"المتاح الحالي: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(total)}", f"الناقص: {_fmt_syp(missing)}", "اشحن محفظتك وجرب تاني 😉"]
            )
            return bot.answer_callback_query(call.id, msg_txt, show_alert=True)

        # ✅ إنشاء حجز ذري بدل الخصم الفوري
        hold_id = None
        try:
            reason = f"حجز إنترنت — {st['provider']} {st['speed']}"
            res = create_hold(uid, total, reason)
            d = getattr(res, "data", None)
            if isinstance(d, dict):
                hold_id = d.get("id") or d.get("hold_id")
            elif isinstance(d, (list, tuple)) and d:
                hold_id = d[0].get("id") if (d and isinstance(d[0], dict)) else (d[0] if d else None)
            elif isinstance(d, (int, str)):
                hold_id = d
        except Exception as e:
            logging.exception(f"[INET][{uid}] create_hold failed: {e}")

        if not hold_id:
            return bot.answer_callback_query(call.id, "⚠️ حصلت مشكلة بسيطة وإحنا بنثبت قيمة العملية. جرّب تاني بعد شوية.", show_alert=True)

        # رسالة للإدارة (موحّدة)
        balance_now = get_balance(uid)
        admin_text = _admin_card([
            "🌐 طلب دفع إنترنت",
            f"👤 الاسم: {call.from_user.full_name}",
            f"يوزر: @{call.from_user.username or ''}",
            f"آيدي: {uid}",
            f"🏷️ المزود: {st['provider']}",
            f"⚡ السرعة: {st['speed']}",
            f"📞 الرقم/الحساب: {st['phone']}",
            f"💰 السعر: {price:,} ل.س",
            f"🧾 العمولة: {comm:,} ل.س",
            f"✅ الإجمالي (محجوز): {total:,} ل.س",
            f"💼 رصيد المستخدم الآن: {balance_now:,} ل.س",
            f"HOLD: {hold_id}"
        ])

        add_pending_request(
            user_id=uid,
            username=call.from_user.username,
            request_text=admin_text,
            payload={
                "type": "internet",
                "provider": st["provider"],
                "speed": st["speed"],
                "phone": st["phone"],
                "price": price,
                "comm": comm,
                "total": total,
                "reserved": total,
                "hold_id": hold_id,   # ✅ مفتاح النجاح في الأدمن
            }
        )
        process_queue(bot)

        # تأكيد للعميل (موحّد)
        ok_txt = _client_card(
            f"✅ تمام يا {nm} — طلبك في السكة 🚀",
            ["بعتنا الطلب للإدارة، التنفيذ عادةً من 1 إلى 4 دقايق (وغالبًا أسرع 😉).",
             "تقدر تبعت طلبات تانية في نفس الوقت — إحنا بنحجز من المتاح بس."]
        )
        bot.answer_callback_query(call.id, "تم الإرسال ✅")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=ok_txt
        )
        st["step"] = "wait_admin"

# شاشة بدء المزودين
def start_internet_provider_menu(bot, message):
    nm = _name(bot, message.from_user.id)
    txt = _client_card(
        f"🌐 يا {nm}، اختار مزوّد الإنترنت",
        [f"💸 العمولة لكل 5000 ل.س: {_fmt_syp(COMMISSION_PER_5000)}"]
    )
    bot.send_message(message.chat.id, txt, reply_markup=_provider_inline_kb())
    user_net_state[message.from_user.id] = {"step": "choose_provider"}
