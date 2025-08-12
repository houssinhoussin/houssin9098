from telebot import types
from services.wallet_service import (
    add_purchase,
    has_sufficient_balance,
    register_user_if_not_exist,
    # هولد
    create_hold,
)
from database.db import get_table
from handlers import keyboards
from services.queue_service import add_pending_request, process_queue
import math  # لإدارة صفحات الكيبورد
import logging

user_states = {}

CASH_TYPES = [
    "تحويل إلى سيرياتيل كاش",
    "تحويل إلى أم تي إن كاش",
    "تحويل إلى شام كاش",
]

CASH_PAGE_SIZE = 3
COMMISSION_PER_50000 = 3500

def _name_of(user):
    # محاولة لطيفة لاستخراج اسم العميل
    return (getattr(user, "full_name", None) or getattr(user, "first_name", None) or "صديقنا").strip()

def _fmt(n):
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

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
    kb.row(*nav)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="commission_cancel"))
    return kb

def calculate_commission(amount):
    blocks = amount // 50000
    remainder = amount % 50000
    commission = blocks * COMMISSION_PER_50000
    if remainder > 0:
        commission += int(COMMISSION_PER_50000 * (remainder / 50000))
    return commission

# التفافات بسيطة للرصيد (نحافظ على بنية ملفك الأصلي)
def get_balance(user_id):
    from services.wallet_service import get_balance as _get
    return _get(user_id)

def start_cash_transfer(bot, message, history=None):
    user_id = message.from_user.id
    register_user_if_not_exist(user_id, _name_of(message.from_user))
    if history is not None:
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
    logging.info(f"[CASH][{user_id}] فتح قائمة تحويل كاش")
    bot.send_message(
        message.chat.id,
        "📤 اختر نوع التحويل من محفظتك:",
        reply_markup=build_cash_menu(0)
    )

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def register(bot, history):

    # تنقّل صفحات أنواع التحويل
    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_page_"))
    def _paginate_cash_menu(call):
        page = int(call.data.split("_")[-1])
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_cash_menu(page)
        )
        bot.answer_callback_query(call.id)

    # اختيار نوع التحويل
    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_sel_"))
    def _cash_type_selected(call):
        idx = int(call.data.split("_")[-1])
        if idx < 0 or idx >= len(CASH_TYPES):
            logging.warning(f"[CASH][{call.from_user.id}] اختيار نوع كاش غير صالح: {idx}")
            bot.answer_callback_query(call.id, "❌ خيار غير صالح.")
            return
        cash_type = CASH_TYPES[idx]
        user_id = call.from_user.id

        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type}")
        name = _name_of(call.from_user)
        text = (
            f"⚠️ يا {name}، تنويه مهم:\n"
            f"• العمولة لكل 50,000 ليرة = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام كمل واكتب الرقم اللي هتحوّل له."
        )
        kb = make_inline_buttons(
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)

    # الدخول من زر بالقائمة الرئيسية (لو عندك زر)
    @bot.message_handler(func=lambda msg: msg.text == "💵 تحويل الى رصيد كاش")
    def open_cash_menu(msg):
        start_cash_transfer(bot, msg, history)

    # نفس الفكرة لكن لو المستخدم كتب نوع التحويل كنص
    @bot.message_handler(func=lambda msg: msg.text in CASH_TYPES)
    def handle_cash_type(msg):
        user_id = msg.from_user.id
        cash_type = msg.text
        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type} (من رسالة)")
        name = _name_of(msg.from_user)
        text = (
            f"⚠️ يا {name}، تنويه مهم:\n"
            f"• العمولة لكل 50,000 ليرة = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام كمل واكتب الرقم اللي هتحوّل له."
        )
        kb = make_inline_buttons(
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        bot.send_message(msg.chat.id, text, reply_markup=kb)

    # إلغاء
    @bot.callback_query_handler(func=lambda call: call.data == "commission_cancel")
    def commission_cancel(call):
        user_id = call.from_user.id
        logging.info(f"[CASH][{user_id}] ألغى عملية التحويل")
        bot.edit_message_text("❌ تم إلغاء العملية. رجعناك للقائمة 👌", call.message.chat.id, call.message.message_id)
        user_states.pop(user_id, None)

    # موافقة على الشروط → اطلب الرقم
    @bot.callback_query_handler(func=lambda call: call.data == "commission_confirm")
    def commission_confirmed(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_number"
        kb = make_inline_buttons(("❌ إلغاء", "commission_cancel"))
        bot.edit_message_text("📲 ابعتلنا الرقم اللي هتحوّل له:", call.message.chat.id, call.message.message_id, reply_markup=kb)

    # استلام الرقم
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_number")
    def get_target_number(msg):
        user_id = msg.from_user.id
        user_states[user_id]["number"] = msg.text.strip()
        user_states[user_id]["step"] = "confirm_number"
        logging.info(f"[CASH][{user_id}] رقم التحويل: {msg.text}")
        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel"),
            ("✏️ تعديل", "edit_number"),
            ("✔️ تأكيد", "number_confirm")
        )
        bot.send_message(
            msg.chat.id,
            f"🔢 الرقم المدخل: {msg.text}\n\nتمام كده؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_number")
    def edit_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_number"
        bot.send_message(call.message.chat.id, "📲 اكتب الرقم من جديد:")

    # بعد تأكيد الرقم → اطلب المبلغ
    @bot.callback_query_handler(func=lambda call: call.data == "number_confirm")
    def number_confirm(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        kb = make_inline_buttons(("❌ إلغاء", "commission_cancel"))
        bot.edit_message_text("💰 اكتب قيمة التحويل المطلوب (بالأرقام):", call.message.chat.id, call.message.message_id, reply_markup=kb)

    # استلام المبلغ وحساب العمولة
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_amount")
    def get_amount_and_confirm(msg):
        user_id = msg.from_user.id
        name = _name_of(msg.from_user)
        try:
            amount = int(msg.text.replace(",", "").strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            logging.warning(f"[CASH][{user_id}] مبلغ غير صالح: {msg.text}")
            bot.send_message(msg.chat.id, f"⚠️ يا {name}، دخّل مبلغ صحيح بالأرقام من غير فواصل.")
            return

        state = user_states[user_id]
        commission = calculate_commission(amount)
        total = amount + commission
        state["amount"] = amount
        state["commission"] = commission
        state["total"] = total
        state["step"] = "confirming"

        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel"),
            ("✏️ تعديل", "edit_amount"),
            ("✔️ تأكيد", "cash_confirm")
        )
        summary = (
            "📤 تأكيد العملية:\n"
            f"• الرقم: {state['number']}\n"
            f"• المبلغ: {_fmt(amount)}\n"
            f"• العمولة: {_fmt(commission)}\n"
            f"• الإجمالي: {_fmt(total)}\n"
            f"• الطريقة: {state['cash_type']}"
        )
        bot.send_message(msg.chat.id, summary, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_amount")
    def edit_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        bot.send_message(call.message.chat.id, "💰 اكتب المبلغ من جديد:")

    # تأكيد نهائي → إنشاء هولد + إرسال للطابور
    @bot.callback_query_handler(func=lambda call: call.data == "cash_confirm")
    def confirm_transfer(call):
        user_id = call.from_user.id
        name = _name_of(call.from_user)

        # منع ازدواج الطلبات
        data = user_states.get(user_id, {}) or {}
        number = data.get("number")
        cash_type = data.get("cash_type")
        amount = int(data.get('amount') or 0)
        commission = int(data.get('commission') or 0)
        total = int(data.get('total') or 0)

        # فحص الرصيد
        available = get_available_balance(user_id)
        if available is None:
            return bot.edit_message_text("❌ حصل خطأ في جلب الرصيد. جرّب تاني.", call.message.chat.id, call.message.message_id)

        if available < total:
            shortage = total - balance
            kb = make_inline_buttons(("💳 شحن المحفظة", "recharge_wallet"), ("⬅️ رجوع", "commission_cancel"))
            return bot.edit_message_text(
                f"❌ يا {name}، متاحك الحالي {_fmt(available)} والمطلوب {_fmt(total)}.\n"
                f"نقصك {_fmt(shortage)} — كمّل شحن ونمشي الطلب سِكة سريعة 😉",
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )

        # إنشاء هولد بدل الخصم الفوري
        hold_desc = f"حجز تحويل كاش — {cash_type} — رقم {number}"
        r = create_hold(user_id, total, hold_desc)
        if getattr(r, "error", None) or not getattr(r, "data", None):
            logging.error(f"[CASH][{user_id}] create_hold failed: {getattr(r, 'error', r)}")
            return bot.edit_message_text("❌ معذرة، ماقدرنا نعمل حجز دلوقتي. جرّب بعد شوية.", call.message.chat.id, call.message.message_id)

        data = getattr(r, "data", None)
        hold_id = (data if isinstance(data, str) else (data.get("id") if isinstance(data, dict) else None))
        # رصيد بعد الحجز (لو متوفر)
        try:
            balance_after = get_balance(user_id)
        except Exception:
            balance_after = None

        # رسالة الإدمن الموحّدة
        admin_msg = (
            f"💰 رصيد المستخدم الآن: {_fmt(balance_after if balance_after is not None else balance)}\n"
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
                "reserved": total,     # للتوافق مع مسارات قديمة
                "hold_id": hold_id,    # المسار الحديث الآمن
                "hold_desc": hold_desc
            }
        )

        # شغّل الطابور
        process_queue(bot)

        # رسالة للعميل
        bot.edit_message_text(
            f"📨 تمام يا {name}! تم إرسال طلبك للإدارة.\n"
            f"⏱️ التنفيذ عادةً خلال 1–4 دقايق.\n"
            f"ℹ️ ملاحظة: تقدر تبعت طلب جديد لحد ما نخلّص الحالي.",
            call.message.chat.id, call.message.message_id
        )
        user_states[user_id]["step"] = "waiting_admin"

    # زر شحن المحفظة
    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet")
    def show_recharge_methods(call):
        bot.send_message(call.message.chat.id, "💳 اختار طريقة شحن المحفظة:", reply_markup=keyboards.recharge_menu())
