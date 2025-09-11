# -*- coding: utf-8 -*-
# handlers/cash_transfer.py — تحويل كاش داخل التطبيق مع /cancel + confirm_guard + رسائل تسويقية
# استيرادات مرنة موجودة عندك
try:
    from anti_spam import too_soon
except Exception:
    try:
        from services.anti_spam import too_soon
    except Exception:
        from handlers.anti_spam import too_soon

try:
    from telegram_safety import remove_inline_keyboard
except Exception:
    try:
        from services.telegram_safety import remove_inline_keyboard
    except Exception:
        from handlers.telegram_safety import remove_inline_keyboard

try:
    from validators import parse_amount
except Exception:
    try:
        from services.validators import parse_amount
    except Exception:
        from handlers.validators import parse_amount

# حارس التأكيد الموحد
try:
    from services.ui_guards import confirm_guard
except Exception:
    # fallback بسيط لو الملف في مسار مختلف
    from ui_guards import confirm_guard

from telebot import types
from services.wallet_service import (
    add_purchase,
    has_sufficient_balance,
    register_user_if_not_exist,
    # هولد
    create_hold,
    # ✅ مهم علشان نتحقق من المتاح (balance - held)
    get_available_balance,
    # لعرض الرصيد في رسالة الإدمن بعد الحجز
    get_balance,
)
from database.db import get_table
from handlers import keyboards
try:
    from services.queue_service import add_pending_request, process_queue
except Exception:
    def add_pending_request(*args, **kwargs):
        return None
    def process_queue(*args, **kwargs):
        return None

import math  # لإدارة صفحات الكيبورد
import logging

# صيانة + أعلام المزايا (Feature Flags)
try:
    from services.system_service import is_maintenance, maintenance_message
except Exception:
    def is_maintenance(): return False
    def maintenance_message(): return "🔧 النظام تحت الصيانة مؤقتًا."

try:
    from services.feature_flags import block_if_disabled  # يتطلب مفتاح "cash_transfer"
except Exception:
    # بديل بسيط: دايمًا يسمح
    def block_if_disabled(bot, chat_id, flag_key, nice_name):
        return False

from services.state_adapter import UserStateDictLike
user_states = UserStateDictLike()
# شاشة واحدة: حرر رسالة البوت بدل الإرسال، واحذف رسالة المستخدم لو موجودة
def _screen(bot, user_id, chat_id, text, reply_markup=None, delete_user_msg_id=None):
    # احذف رسالة المستخدم (لإبقاء شاشة واحدة)
    try:
        if delete_user_msg_id:
            bot.delete_message(chat_id, delete_user_msg_id)
    except Exception:
        pass

    st = user_states.get(user_id, {}) or {}
    last_id = st.get("last_bot_msg_id")

    # جرّب التحرير أولًا
    if last_id:
        try:
            bot.edit_message_text(text, chat_id, last_id, reply_markup=reply_markup)
            return
        except Exception:
            # ممكن الرسالة لا تقبل التحرير أو اختفت — نرسل جديدة
            pass

    m = bot.send_message(chat_id, text, reply_markup=reply_markup)
    st["last_bot_msg_id"] = m.message_id
    user_states[user_id] = st

def _screen_from_call(bot, call, text, reply_markup=None):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _screen(bot, call.from_user.id, call.message.chat.id, text, reply_markup)

CASH_TYPES = [
    "تحويل إلى سيرياتيل كاش",
    "تحويل إلى أم تي إن كاش",
    "تحويل إلى شام كاش",
]

CASH_PAGE_SIZE = 3
COMMISSION_PER_50000 = 3500

# ===== مظهر الرسائل + /cancel =====
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."

def banner(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def with_cancel_hint(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _name_of(user):
    # محاولة لطيفة لاستخراج اسم العميل
    return (getattr(user, "full_name", None) or getattr(user, "first_name", None) or "صديقنا").strip()

def _fmt(n):
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _service_unavailable_guard(bot, chat_id) -> bool:
    """يرجع True إذا كانت الخدمة غير متاحة (صيانة أو متوقفة عبر Feature Flag)."""
    if is_maintenance():
        bot.send_message(chat_id, maintenance_message())
        return True
    if block_if_disabled(bot, chat_id, "cash_transfer", "تحويل كاش"):
        return True
    return False

def build_cash_menu(page: int = 0):
    total = len(CASH_TYPES)
    pages = max(1, math.ceil(total / CASH_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    start = page * CASH_PAGE_SIZE
    end = start + CASH_PAGE_SIZE
    for idx, label in enumerate(CASH_TYPES[start:end], start=start):
        kb.add(types.InlineKeyboardButton(label, callback_data=f"cash_sel_{idx}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"cash_page_{page-1}"))
    nav.append(types.InlineKeyboardButton(f"{page+1}/{pages}", callback_data="cash_noop"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"cash_page_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="commission_cancel"))
    return kb

def calculate_commission(amount: int) -> int:
    # حساب بالعدد الصحيح لتفادي float
    blocks = amount // 50000
    remainder = amount % 50000
    commission = blocks * COMMISSION_PER_50000
    # جزء نسبي من العمولة
    commission += (remainder * COMMISSION_PER_50000) // 50000
    return int(commission)

# التفافات بسيطة للرصيد (نحافظ على بنية ملفك الأصلي)
def get_balance_local(user_id):
    from services.wallet_service import get_balance as _get
    return _get(user_id)

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def start_cash_transfer(bot, message, history=None):
    user_id = message.from_user.id
    register_user_if_not_exist(user_id, _name_of(message.from_user))
    # حارس توفر الخدمة
    if _service_unavailable_guard(bot, message.chat.id):
        return
    if history is not None:
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
    logging.info(f"[CASH][{user_id}] فتح قائمة تحويل كاش")
    _screen(bot, user_id, message.chat.id,
        with_cancel_hint("💸 جاهز نحرك الفلوس؟ اختار نوع التحويل من محفظتك:"),
        reply_markup=build_cash_menu(0))

def register(bot, history):

    # ===== /cancel العام =====
    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(msg):
        uid = msg.from_user.id
        user_states.pop(uid, None)
        _screen(bot, uid, msg.chat.id,
                banner("❌ تم الإلغاء", [f"يا {_name_of(msg.from_user)}، رجعناك للقائمة. اختار اللي يناسبك 👇"]),
                reply_markup=build_cash_menu(0))


    # تنقّل صفحات أنواع التحويل
    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_page_"))
    def _paginate_cash_menu(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        page = int(call.data.split("_")[-1])
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_cash_menu(page)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    # زر عدّاد صفحات (لا شيء)
    @bot.callback_query_handler(func=lambda c: c.data == "cash_noop")
    def _noop(call):
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_menu")
    def back_to_menu(call):
        uid = call.from_user.id
        user_states[uid] = {"step": "show_commission"}  # نرجّعه لمرحلة ما قبل إدخال الرقم
        _screen_from_call(bot, call,
            with_cancel_hint("💸 اختار نوع التحويل من محفظتك:"),
            reply_markup=build_cash_menu(0))

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_number")
    def back_to_number(call):
        uid = call.from_user.id
        st = user_states.get(uid, {}) or {}
        st["step"] = "awaiting_number"
        user_states[uid] = st
        _screen_from_call(bot, call,
            with_cancel_hint("📲 اكتب الرقم من جديد:"),
            reply_markup=make_inline_buttons(("⬅️ رجوع","back_to_menu"), ("❌ إلغاء","commission_cancel")))

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_amount")
    def back_to_amount(call):
        uid = call.from_user.id
        st = user_states.get(uid, {}) or {}
        st["step"] = "awaiting_amount"
        user_states[uid] = st
        _screen_from_call(bot, call,
            with_cancel_hint("💰 اكتب قيمة التحويل المطلوب (بالأرقام):"),
            reply_markup=make_inline_buttons(("⬅️ رجوع","back_to_number"), ("❌ إلغاء","commission_cancel")))

    # اختيار نوع التحويل
    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_sel_"))
    def _cash_type_selected(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        idx = int(call.data.split("_")[-1])
        if idx < 0 or idx >= len(CASH_TYPES):
            logging.warning(f"[CASH][{call.from_user.id}] اختيار نوع كاش غير صالح: {idx}")
            bot.answer_callback_query(call.id, "❌ خيار غير صالح.")
            return
        cash_type = CASH_TYPES[idx]
        user_id = call.from_user.id

        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if history is not None:
            if not isinstance(history.get(user_id), list):
                history[user_id] = []
            history[user_id].append("cash_menu")

        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type}")
        name = _name_of(call.from_user)
        text = with_cancel_hint(
            f"⚠️ يا {name}، تنويه مهم:\n"
            f"• العمولة لكل 50,000 ليرة = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام، دوس موافق وكمل اكتب الرقم اللي هتحوّل له."
        )
        kb = make_inline_buttons(
            ("⬅️ رجوع", "back_to_menu"),
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel"),
        )
        _screen_from_call(bot, call, text, reply_markup=kb)

    # الدخول من زر بالقائمة الرئيسية (لو عندك زر)
    @bot.message_handler(func=lambda msg: msg.text == "💵 تحويل الى رصيد كاش")
    def open_cash_menu(msg):
        # ✅ إنهاء أي رحلة/مسار سابق عالق
        try:
            from handlers.start import _reset_user_flows
            _reset_user_flows(msg.from_user.id)
        except Exception:
            pass

        start_cash_transfer(bot, msg, history)
    # نفس الفكرة لكن لو المستخدم كتب نوع التحويل كنص
    @bot.message_handler(func=lambda msg: msg.text in CASH_TYPES)
    def handle_cash_type(msg):
        user_id = msg.from_user.id
        # حارس توفر الخدمة
        if _service_unavailable_guard(bot, msg.chat.id):
            return
        cash_type = msg.text
        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if history is not None:
            if not isinstance(history.get(user_id), list):
                history[user_id] = []
            history[user_id].append("cash_menu")

        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type} (من رسالة)")
        name = _name_of(msg.from_user)
        text = with_cancel_hint(
            f"⚠️ يا {name}، تنويه مهم:\n"
            f"• العمولة لكل 50,000 ليرة = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام، دوس موافق وكمل اكتب الرقم اللي هتحوّل له."
        )
        kb = make_inline_buttons(
            ("⬅️ رجوع", "back_to_menu"),
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel"),
        )
        _screen(bot, user_id, msg.chat.id, text, reply_markup=kb, delete_user_msg_id=msg.message_id)

    # إلغاء
    @bot.callback_query_handler(func=lambda call: call.data == "commission_cancel")
    def commission_cancel(call):
        user_id = call.from_user.id
        logging.info(f"[CASH][{user_id}] ألغى عملية التحويل")
        user_states.pop(user_id, None)
        _screen_from_call(bot, call,
            banner("❌ تم الإلغاء", ["رجعناك للقائمة الرئيسية 👇"]),
            reply_markup=build_cash_menu(0))

    # موافقة على الشروط → اطلب الرقم
    @bot.callback_query_handler(func=lambda call: call.data == "commission_confirm")
    def commission_confirmed(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)

        user_id = call.from_user.id
        # ننتقل لمرحلة إدخال الرقم
        st = user_states.get(user_id, {}) or {}
        st["step"] = "awaiting_number"
        user_states[user_id] = st

        kb = make_inline_buttons(("⬅️ رجوع", "back_to_menu"), ("❌ إلغاء", "commission_cancel"))
        _screen_from_call(bot, call, with_cancel_hint("📲 ابعتلنا الرقم اللي هتحوّل له:"), reply_markup=kb)

    # استلام الرقم
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_number")
    def get_target_number(msg):
        user_id = msg.from_user.id
        number = (msg.text or "").strip()
        user_states[user_id] = {**user_states.get(user_id, {}), "number": number, "step": "confirm_number"}
        logging.info(f"[CASH][{user_id}] رقم التحويل: {number}")
        kb = make_inline_buttons(
            ("⬅️ رجوع", "back_to_menu"),
            ("✏️ تعديل", "edit_number"),
            ("✔️ تأكيد", "number_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        _screen(bot, user_id, msg.chat.id,
                with_cancel_hint(f"🔢 الرقم المدخل: {number}\n\nتمام كده؟"),
                reply_markup=kb, delete_user_msg_id=msg.message_id)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_number")
    def edit_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_number"
        _screen_from_call(bot, call, with_cancel_hint("📲 اكتب الرقم من جديد:"),
                  reply_markup=make_inline_buttons(("⬅️ رجوع","back_to_menu"), ("❌ إلغاء","commission_cancel")))

    # بعد تأكيد الرقم → اطلب المبلغ
    @bot.callback_query_handler(func=lambda call: call.data == "number_confirm")
    def number_confirm(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        kb = make_inline_buttons(("⬅️ رجوع", "back_to_number"), ("❌ إلغاء", "commission_cancel"))
        _screen_from_call(bot, call, with_cancel_hint("💰 اكتب قيمة التحويل المطلوب (بالأرقام):"), reply_markup=kb)

    # استلام المبلغ وحساب العمولة
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_amount")
    def get_amount_and_confirm(msg):
        user_id = msg.from_user.id
        name = _name_of(msg.from_user)
        amount_text = (msg.text or "").strip()
        try:
            amount = parse_amount(amount_text, min_value=1)
        except Exception:
            logging.warning(f"[CASH][{user_id}] مبلغ غير صالح: {msg.text}")
            _screen(bot, user_id, msg.chat.id,
                    with_cancel_hint(f"⚠️ يا {name}، دخّل مبلغ صحيح بالأرقام من غير فواصل/رموز."),
                    delete_user_msg_id=msg.message_id)
            return

        state = user_states.get(user_id, {}) or {}
        commission = calculate_commission(amount)
        total = amount + commission
        state.update({"amount": amount, "commission": commission, "total": total, "step": "confirming"})
        user_states[user_id] = state

        summary = banner(
            "📤 تأكيد العملية",
            [
                f"• الرقم: {state['number']}",
                f"• المبلغ: {_fmt(amount)}",
                f"• العمولة: {_fmt(commission)}",
                f"• الإجمالي: {_fmt(total)}",
                f"• الطريقة: {state['cash_type']}"
            ]
        )
        kb = make_inline_buttons(
            ("⬅️ رجوع", "back_to_amount"),
            ("✏️ تعديل", "edit_amount"),
            ("✔️ تأكيد", "cash_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        _screen(bot, user_id, msg.chat.id, with_cancel_hint(summary),
                reply_markup=kb, delete_user_msg_id=msg.message_id)


    @bot.callback_query_handler(func=lambda call: call.data == "edit_amount")
    def edit_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        _screen_from_call(bot, call, with_cancel_hint("💰 اكتب المبلغ من جديد:"),
                          reply_markup=make_inline_buttons(("⬅️ رجوع","back_to_number"), ("❌ إلغاء","commission_cancel")))

    # تأكيد نهائي → إنشاء هولد + إرسال للطابور
    
    @bot.callback_query_handler(func=lambda call: call.data == "cash_confirm")
    def confirm_transfer(call):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

        user_id = call.from_user.id
        name = _name_of(call.from_user)

        # Debounce
        if confirm_guard(bot, call, "cash_confirm"):
            return

        if _service_unavailable_guard(bot, call.message.chat.id):
            return

        data = user_states.get(user_id, {}) or {}
        number = data.get("number")
        cash_type = data.get("cash_type")
        amount = int(data.get('amount') or 0)
        commission = int(data.get('commission') or 0)
        total = int(data.get('total') or 0)

        available = get_available_balance(user_id)
        if available is None:
            return _screen_from_call(bot, call, "❌ حصل خطأ في جلب الرصيد. جرّب تاني.\n\n" + CANCEL_HINT)

        if available < total:
            shortage = total - available
            kb = make_inline_buttons(("💳 شحن المحفظة", "recharge_wallet"), ("⬅️ رجوع", "back_to_amount"))
            return _screen_from_call(
                bot, call,
                with_cancel_hint(
                    f"❌ يا {name}، متاحك الحالي {_fmt(available)} والمطلوب {_fmt(total)}.\n"
                    f"نقصك {_fmt(shortage)} — كمّل شحن ونمشي الطلب سِكة سريعة 😉"
                ),
                reply_markup=kb
            )

        hold_desc = f"حجز تحويل كاش — {cash_type} — رقم {number}"
        try:
            r = create_hold(user_id, total, hold_desc)
        except Exception as e:
            logging.exception(f"[CASH][{user_id}] create_hold exception: {e}")
            return _screen_from_call(bot, call, "❌ معذرة، ماقدرنا نعمل حجز دلوقتي. جرّب بعد شوية.\n\n" + CANCEL_HINT)

        if getattr(r, "error", None) or not getattr(r, "data", None):
            logging.error(f"[CASH][{user_id}] create_hold failed: {getattr(r, 'error', r)}")
            return _screen_from_call(bot, call, "❌ معذرة، ماقدرنا نعمل حجز دلوقتي. جرّب بعد شوية.\n\n" + CANCEL_HINT)

        data_resp = getattr(r, "data", None)
        if isinstance(data_resp, dict):
            hold_id = data_resp.get("id") or data_resp.get("hold_id") or data_resp
        elif isinstance(data_resp, (list, tuple)) and data_resp:
            first = data_resp[0]
            hold_id = first.get("id") if isinstance(first, dict) else first
        else:
            hold_id = data_resp

        try:
            balance_after = get_balance(user_id)
        except Exception:
            balance_after = None

        admin_msg = (
            f"💰 رصيد المستخدم الآن: {_fmt(balance_after) if balance_after is not None else '—'}\n"
            f"🆕 طلب جديد — تحويل كاش\n"
            f"👤 الاسم: <code>{_name_of(call.from_user)}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🔖 النوع: {cash_type}\n"
            f"📲 رقم المستفيد: <code>{number}</code>\n"
            f"💸 المبلغ: {_fmt(amount)}\n"
            f"🧾 العمولة: {_fmt(commission)}\n"
            f"✅ الإجمالي: {_fmt(total)}\n"
            f"🔒 HOLD: <code>{hold_id}</code>"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "cash_transfer",
                "number": number,
                "cash_type": cash_type,
                "amount": amount,
                "commission": commission,
                "total": total,
                "reserved": total,
                "hold_id": hold_id,
                "hold_desc": hold_desc
            }
        )

        process_queue(bot)
        # 🧾 أرسل إيصال ثابت للعميل (لن يتم تحريره لاحقًا — يبقى كمرجع)
        receipt_text = banner(
            "🧾 إيصال طلب تحويلك",
            [
                f"• النوع: {cash_type}",
                f"• رقم المستفيد: {number}",
                f"• المبلغ: {_fmt(amount)}",
                f"• العمولة: {_fmt(commission)}",
                f"• الإجمالي: {_fmt(total)}",
                f"• معرف الحجز (HOLD): {hold_id}",
            ]
        ) + "\n\nاحتفظ بهذه الرسالة كمرجع. في حال حدوث أي مشكلة: «انظر، أنت أرسلت نفس هذه التفاصيل»."
        try:
            bot.send_message(call.message.chat.id, receipt_text)
        except Exception:
            logging.exception(f"[CASH][{user_id}] failed to send receipt")

        _screen_from_call(
            bot, call,
            banner(
                f"✅ تمام يا {name}! بعتنا طلب تحويلك 🚀",
                [
                    "⏱️ التنفيذ عادةً خلال 1–4 دقايق.",
                    "ℹ️ تقدر تبعت طلب جديد لو حابب — كل الطلبات بتحترم الرصيد المتاح 😉",
                ]
            ),
            reply_markup=None
        )
        user_states[user_id] = {**user_states.get(user_id, {}) , "step": "waiting_admin"}

    # زر شحن المحفظة
    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet")
    def show_recharge_methods(call):
        _screen_from_call(
            bot, call,
            "💳 اختار طريقة شحن المحفظة:",
            reply_markup=keyboards.recharge_menu()
        )
