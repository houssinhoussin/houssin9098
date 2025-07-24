from telebot import types
import math  # added for pagination support
import logging
from services.wallet_service import (
    get_balance,
    deduct_balance,
    add_balance,
    register_user_if_not_exist,
    add_purchase,
    has_sufficient_balance,
)
from config import ADMIN_MAIN_ID
from services.queue_service import add_pending_request, process_queue, delete_pending_request
from database.db import get_table         # ← هنا

# --- قوائم المنتجات (وحدات) وأسعارها (لم يتم تعديل القيم) ---
SYRIATEL_UNITS = [
    {"name": "1000 وحدة", "price": 1200},
    {"name": "1500 وحدة", "price": 1800},
    {"name": "2013 وحدة", "price": 2400},
    {"name": "3068 وحدة", "price": 3682},
    {"name": "4506 وحدة", "price": 5400},
    {"name": "5273 وحدة", "price": 6285},
    {"name": "7190 وحدة", "price": 8628},
    {"name": "9587 وحدة", "price": 11500},
    {"name": "13039 وحدة", "price": 15500},
]

MTN_UNITS = [
    {"name": "1000 وحدة", "price": 1200},
    {"name": "5000 وحدة", "price": 6000},
    {"name": "7000 وحدة", "price": 8400},
    {"name": "10000 وحدة", "price": 12000},
    {"name": "15000 وحدة", "price": 18000},
    {"name": "20000 وحدة", "price": 24000},
    {"name": "23000 وحدة", "price": 27600},
    {"name": "30000 وحدة", "price": 36000},
    {"name": "36000 وحدة", "price": 43200},
]

user_states = {}

# -------------------- أدوات مساعدة عامة --------------------

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def _unit_label(unit: dict) -> str:
    return f"{unit['name']} - {unit['price']:,} ل.س"

# لوحة Reply القديمة (للخلفية/التوافق)
def units_bills_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("🔴 وحدات سيرياتيل"),
        types.KeyboardButton("🔴 فاتورة سيرياتيل"),
        types.KeyboardButton("🟡 وحدات MTN"),
        types.KeyboardButton("🟡 فاتورة MTN"),
    )
    kb.add(types.KeyboardButton("⬅️ رجوع"))
    return kb

# النسخة الجديدة: InlineKeyboard أساسي
def units_bills_menu_inline():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔴 وحدات سيرياتيل", callback_data="ubm:syr_units"))
    kb.add(types.InlineKeyboardButton("🔴 فاتورة سيرياتيل", callback_data="ubm:syr_bill"))
    kb.add(types.InlineKeyboardButton("🟡 وحدات MTN", callback_data="ubm:mtn_units"))
    kb.add(types.InlineKeyboardButton("🟡 فاتورة MTN", callback_data="ubm:mtn_bill"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ubm:back"))
    return kb

# باني كيبورد صفحات عام
def _build_paged_inline_keyboard(items, page: int = 0, page_size: int = 5, prefix: str = "pg", back_data: str | None = None):
    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    end = start + page_size
    slice_items = items[start:end]

    kb = types.InlineKeyboardMarkup()
    for idx, label in slice_items:
        kb.add(types.InlineKeyboardButton(label, callback_data=f"{prefix}:sel:{idx}"))

    # navigation row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"{prefix}:page:{page-1}"))
    nav.append(types.InlineKeyboardButton(f"{page+1}/{pages}", callback_data=f"{prefix}:noop"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"{prefix}:page:{page+1}"))
    if nav:
        kb.row(*nav)

    if back_data:
        kb.add(types.InlineKeyboardButton("🔙 رجوع", callback_data=back_data))

    return kb, pages


def register_bill_and_units(bot, history):
    """تسجيل جميع هاندلرات خدمات (وحدات/فواتير) لكل من سيرياتيل و MTN.
    تم إضافة دعم InlineKeyboard مع Pagination ودعم حجز الرصيد.
    كل الهاندلرات الأصلية (القائمة على ReplyKeyboard) باقية كما هي للتوافق.
    """

    # ===== القائمة الرئيسية للخدمة =====
    @bot.message_handler(func=lambda msg: msg.text == "💳 تحويل وحدات فاتورة سوري")
    def open_main_menu(msg):
        user_id = msg.from_user.id
        history.setdefault(user_id, []).append("units_bills_menu")
        user_states[user_id] = {"step": None}
        # تم استبدال لوحة الرد بلوحة إنلاين
        bot.send_message(msg.chat.id, "اختر الخدمة:", reply_markup=units_bills_menu_inline())

    # --------- Router له واجهة الإنلاين الرئيسية ---------
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ubm:")) 
    def ubm_router(call):
        action = call.data.split(":", 1)[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "syr_units":
            user_states[user_id] = {"step": "select_syr_unit"}
            _send_syr_units_page(chat_id, page=0, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        if action == "syr_bill":
            user_states[user_id] = {"step": "syr_bill_number"}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 أدخل رقم سيرياتيل المراد دفع فاتورته:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        if action == "mtn_units":
            user_states[user_id] = {"step": "select_mtn_unit"}
            _send_mtn_units_page(chat_id, page=0, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        if action == "mtn_bill":
            user_states[user_id] = {"step": "mtn_bill_number"}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 أدخل رقم MTN المراد دفع فاتورته:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        if action == "back":
            try:
                from keyboards import main_menu as _main_menu
                bot.edit_message_text("⬅️ رجوع", chat_id, call.message.message_id)
                bot.send_message(chat_id, "اختر من القائمة:", reply_markup=_main_menu())
            except Exception:
                bot.edit_message_text("⬅️ رجوع", chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    # ---------- أدوات إرسال قوائم الوحدات (Inline + Pagination) ----------
    PAGE_SIZE_UNITS = 5

    def _send_syr_units_page(chat_id, page=0, message_id=None):
        items = [(idx, _unit_label(u)) for idx, u in enumerate(SYRIATEL_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="syrunits", back_data="ubm:back")
        text = f"اختر كمية الوحدات (صفحة {page+1}/{pages}):"
        if message_id is not None:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)

    def _send_mtn_units_page(chat_id, page=0, message_id=None):
        items = [(idx, _unit_label(u)) for idx, u in enumerate(MTN_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="mtnunits", back_data="ubm:back")
        text = f"اختر كمية الوحدات (صفحة {page+1}/{pages}):"
        if message_id is not None:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)

    # ------ ملاحق كولباك للوحدات (سيرياتيل) ------
    @bot.callback_query_handler(func=lambda call: call.data.startswith("syrunits:"))
    def syr_units_inline_handler(call):
        parts = call.data.split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "page":
            page = int(parts[2]) if len(parts)>2 else 0
            _send_syr_units_page(chat_id, page=page, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        if action == "sel":
            idx = int(parts[2])
            unit = SYRIATEL_UNITS[idx]
            user_states[user_id] = {"step": "syr_unit_number", "unit": unit}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 أدخل الرقم أو الكود الذي يبدأ بـ 093 أو 098 أو 099:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id, text=_unit_label(unit))
            return

        if action == "back":
            bot.edit_message_text("اختر الخدمة:", chat_id, call.message.message_id, reply_markup=units_bills_menu_inline())
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    # ------ ملاحق كولباك للوحدات (MTN) ------
    @bot.callback_query_handler(func=lambda call: call.data.startswith("mtnunits:"))
    def mtn_units_inline_handler(call):
        parts = call.data.split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "page":
            page = int(parts[2]) if len(parts)>2 else 0
            _send_mtn_units_page(chat_id, page=page, message_id=call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        if action == "sel":
            idx = int(parts[2])
            unit = MTN_UNITS[idx]
            user_states[user_id] = {"step": "mtn_unit_number", "unit": unit}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 أدخل الرقم أو الكود الذي يبدأ بـ 094 أو 095 أو 096:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id, text=_unit_label(unit))
            return

        if action == "back":
            bot.edit_message_text("اختر الخدمة:", chat_id, call.message.message_id, reply_markup=units_bills_menu_inline())
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    ########## وحدات سيرياتيل ##########
    @bot.message_handler(func=lambda msg: msg.text == "🔴 وحدات سيرياتيل")
    def syr_units_menu(msg):
        user_id = msg.from_user.id
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for u in SYRIATEL_UNITS:
            kb.add(types.KeyboardButton(_unit_label(u)))
        kb.add(types.KeyboardButton("⬅️ رجوع"))
        user_states[user_id] = {"step": "select_syr_unit"}
        bot.send_message(msg.chat.id, "اختر كمية الوحدات:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "select_syr_unit")
    def syr_unit_select(msg):
        user_id = msg.from_user.id
        unit = next((u for u in SYRIATEL_UNITS if _unit_label(u) == msg.text), None)
        if not unit:
            bot.send_message(msg.chat.id, "⚠️ اختر كمية من القائمة.")
            return
        user_states[user_id] = {"step": "syr_unit_number", "unit": unit}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 أدخل الرقم أو الكود الذي يبدأ بـ 093 أو 098 أو 099:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_unit_number")
    def syr_unit_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        state = user_states[user_id]
        state["number"] = number
        state["step"] = "syr_unit_confirm"
        unit = state["unit"]
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✔️ تأكيد الشراء", "syr_unit_final_confirm")
        )
        bot.send_message(
            msg.chat.id,
            f"هل أنت متأكد من شراء {unit['name']} بسعر {unit['price']:,} ل.س للرقم:\n{number}؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "syr_unit_final_confirm")
    def syr_unit_final_confirm(call):
        user_id = call.from_user.id

        existing = get_table("pending_requests") \
            .select("id") \
            .eq("user_id", user_id) \
            .execute()
        if existing.data:
            return bot.send_message(call.message.chat.id,
                "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )

        state = user_states[user_id]
        price = state["unit"]["price"]

        balance = get_balance(user_id)
        if balance < price:
            bot.send_message(call.message.chat.id,
                f"❌ لا يوجد رصيد كافٍ في محفظتك.\nرصيدك: {balance:,} ل.س\nالمطلوب: {price:,} ل.س"
            )
            return

        deduct_balance(user_id, price)

        state["step"] = "wait_admin_syr_unit"
        summary = (
            f"🔴 طلب وحدات سيرياتيل:\n"
            f"👤 المستخدم: {user_id}\n"
            f"📱 {state['number']}\n"
            f"💵 {state['unit']['name']}\n"
            f"💰 {price:,} ل.س"
        )
        print(f"[DEBUG] Adding pending syr unit request with reserved amount: {price}")
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=summary,
            payload={
                "type": "syr_unit",
                "number": state["number"],
                "unit_name": state["unit"]["name"],
                "price": price,
                "reserved": price,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, "✅ تم إرسال طلبك للإدارة، بانتظار الموافقة.")

    ########## وحدات MTN ##########
    @bot.message_handler(func=lambda msg: msg.text == "🟡 وحدات MTN")
    def mtn_units_menu(msg):
        user_id = msg.from_user.id
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for u in MTN_UNITS:
            kb.add(types.KeyboardButton(_unit_label(u)))
        kb.add(types.KeyboardButton("⬅️ رجوع"))
        user_states[user_id] = {"step": "select_mtn_unit"}
        bot.send_message(msg.chat.id, "اختر كمية الوحدات:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "select_mtn_unit")
    def mtn_unit_select(msg):
        user_id = msg.from_user.id
        unit = next((u for u in MTN_UNITS if _unit_label(u) == msg.text), None)
        if not unit:
            bot.send_message(msg.chat.id, "⚠️ اختر كمية من القائمة.")
            return
        user_states[user_id] = {"step": "mtn_unit_number", "unit": unit}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 أدخل الرقم أو الكود الذي يبدأ بـ 094 أو 095 أو 096:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_unit_number")
    def mtn_unit_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        state = user_states[user_id]
        state["number"] = number
        state["step"] = "mtn_unit_confirm"
        unit = state["unit"]
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✔️ تأكيد الشراء", "mtn_unit_final_confirm")
        )
        bot.send_message(
            msg.chat.id,
            f"هل أنت متأكد من شراء {unit['name']} بسعر {unit['price']:,} ل.س للرقم:\n{number}؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "mtn_unit_final_confirm")
    def mtn_unit_final_confirm(call):
        user_id = call.from_user.id

        existing = get_table("pending_requests") \
            .select("id") \
            .eq("user_id", user_id) \
            .execute()
        if existing.data:
            return bot.send_message(call.message.chat.id,
                "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )

        state = user_states[user_id]
        price = state["unit"]["price"]

        balance = get_balance(user_id)
        if balance < price:
            bot.send_message(call.message.chat.id,
                f"❌ لا يوجد رصيد كافٍ في محفظتك.\nرصيدك: {balance:,} ل.س\nالمطلوب: {price:,} ل.س"
            )
            return

        deduct_balance(user_id, price)

        state["step"] = "wait_admin_mtn_unit"
        summary = (
            f"🟡 طلب وحدات MTN:\n"
            f"👤 المستخدم: {user_id}\n"
            f"📱 {state['number']}\n"
            f"💵 {state['unit']['name']}\n"
            f"💰 {price:,} ل.س"
        )
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=summary,
            payload={
                "type": "mtn_unit",
                "number": state["number"],
                "unit_name": state["unit"]["name"],
                "price": price,
                "reserved": price,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, "✅ تم إرسال طلبك للإدارة، بانتظار الموافقة.")

    ########## فاتورة سيرياتيل ##########
    @bot.message_handler(func=lambda msg: msg.text == "🔴 فاتورة سيرياتيل")
    def syr_bill_entry(msg):
        user_id = msg.from_user.id
        user_states[user_id] = {"step": "syr_bill_number"}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 أدخل رقم سيرياتيل المراد دفع فاتورته:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_bill_number")
    def syr_bill_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        user_states[user_id]["number"] = number
        user_states[user_id]["step"] = "syr_bill_number_confirm"
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✏️ تعديل", "edit_syr_bill_number"),
            ("✔️ تأكيد", "confirm_syr_bill_number")
        )
        bot.send_message(msg.chat.id, f"هل الرقم التالي صحيح؟\n{number}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_number")
    def edit_syr_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_number"
        bot.send_message(call.message.chat.id, "📱 أعد إدخال رقم الموبايل:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_number")
    def confirm_syr_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(call.message.chat.id, "💵 أدخل مبلغ الفاتورة بالليرة:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_bill_amount")
    def syr_bill_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except:
            bot.send_message(msg.chat.id, "⚠️ أدخل مبلغ صحيح.")
            return
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "syr_bill_amount_confirm"
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✏️ تعديل", "edit_syr_bill_amount"),
            ("✔️ تأكيد", "confirm_syr_bill_amount")
        )
        bot.send_message(
            msg.chat.id,
            f"هل المبلغ التالي صحيح؟\n{amount:,} ل.س", reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_amount")
    def edit_syr_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        bot.send_message(call.message.chat.id, "💵 أعد إرسال مبلغ الفاتورة:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_amount")
    def confirm_syr_bill_amount(call):
        user_id = call.from_user.id
        amount = user_states[user_id]["amount"]
        amount_with_fee = int(amount * 1.10)
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "syr_bill_final_confirm"
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✔️ تأكيد", "final_confirm_syr_bill")
        )
        bot.send_message(
            call.message.chat.id,
            f"سيتم دفع فاتورة سيرياتيل للرقم: {user_states[user_id]['number']}\n"
            f"المبلغ: {amount:,} ل.س\n"
            f"أجور التحويل : {amount_with_fee-amount:,} ل.س\n"
            f"الإجمالي: {amount_with_fee:,} ل.س\n"
            "هل تريد المتابعة؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_syr_bill")
    def final_confirm_syr_bill(call):
        user_id = call.from_user.id

        existing = get_table("pending_requests") \
            .select("id") \
            .eq("user_id", user_id) \
            .execute()
        if existing.data:
            return bot.send_message(call.message.chat.id,
                "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )

        total = user_states[user_id]["amount_with_fee"]
        balance = get_balance(user_id)
        if balance < total:
            kb = make_inline_buttons(
                ("❌ إلغاء", "cancel_all"),
                ("💼 المحفظة", "go_wallet")
            )
            bot.send_message(call.message.chat.id,
                f"❌ رصيد غير كافٍ.\nرصيدك: {balance:,} ل.س\n"
                f"المطلوب: {total:,} ل.س\n"
                f"الناقص: {total - balance:,} ل.س",
                reply_markup=kb
            )
            return

        deduct_balance(user_id, total)

        state = user_states[user_id]
        state["step"] = "wait_admin_syr_bill"
        summary = (
            f"🔴 طلب دفع فاتورة سيرياتيل:\n"
            f"👤 المستخدم: {user_id}\n"
            f"📱 {state['number']}\n"
            f"💵 {state['amount']:,} ل.س\n"
            f"🧾 مع العمولة: {total:,} ل.س"
        )
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=summary,
            payload={
                "type": "syr_bill",
                "number": state["number"],
                "amount": state["amount"],
                "total": total,
                "reserved": total,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, "✅ تم إرسال طلبك للإدارة، بانتظار الموافقة.")

    ########## فاتورة MTN ##########
    @bot.message_handler(func=lambda msg: msg.text == "🟡 فاتورة MTN")
    def mtn_bill_entry(msg):
        user_id = msg.from_user.id
        user_states[user_id] = {"step": "mtn_bill_number"}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 أدخل رقم MTN المراد دفع فاتورته:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_bill_number")
    def mtn_bill_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        user_states[user_id]["number"] = number
        user_states[user_id]["step"] = "mtn_bill_number_confirm"
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✏️ تعديل", "edit_mtn_bill_number"),
            ("✔️ تأكيد", "confirm_mtn_bill_number")
        )
        bot.send_message(msg.chat.id, f"هل الرقم التالي صحيح؟\n{number}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_number")
    def edit_mtn_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_number"
        bot.send_message(call.message.chat.id, "📱 أعد إدخال رقم الموبايل:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_number")
    def confirm_mtn_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(call.message.chat.id, "💵 أدخل مبلغ الفاتورة بالليرة:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_bill_amount")
    def mtn_bill_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except:
            bot.send_message(msg.chat.id, "⚠️ أدخل مبلغ صحيح.")
            return
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "mtn_bill_amount_confirm"
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✏️ تعديل", "edit_mtn_bill_amount"),
            ("✔️ تأكيد", "confirm_mtn_bill_amount")
        )
        bot.send_message(
            msg.chat.id,
            f"هل المبلغ التالي صحيح؟\n{amount:,} ل.س", reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_amount")
    def edit_mtn_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        bot.send_message(call.message.chat.id, "💵 أعد إرسال مبلغ الفاتورة:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_amount")
    def confirm_mtn_bill_amount(call):
        user_id = call.from_user.id
        amount = user_states[user_id]["amount"]
        amount_with_fee = int(amount * 1.10)
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "mtn_bill_final_confirm"
        kb = make_inline_buttons(
            ("❌ إلغاء", "cancel_all"),
            ("✔️ تأكيد", "final_confirm_mtn_bill")
        )
        bot.send_message(
            call.message.chat.id,
            f"سيتم دفع فاتورة MTN للرقم: {user_states[user_id]['number']}\n"
            f"المبلغ: {amount:,} ل.س\n"
            f"أجور التحويل : {amount_with_fee-amount:,} ل.س\n"
            f"الإجمالي: {amount_with_fee:,} ل.س\n"
            "هل تريد المتابعة؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_mtn_bill")
    def final_confirm_mtn_bill(call):
        user_id = call.from_user.id

        existing = get_table("pending_requests") \
            .select("id") \
            .eq("user_id", user_id) \
            .execute()
        if existing.data:
            return bot.send_message(call.message.chat.id,
                "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )

        total = user_states[user_id]["amount_with_fee"]
        balance = get_balance(user_id)
        if balance < total:
            kb = make_inline_buttons(
                ("❌ إلغاء", "cancel_all"),
                ("💼 المحفظة", "go_wallet")
            )
            bot.send_message(call.message.chat.id,
                f"❌ رصيد غير كافٍ.\nرصيدك: {balance:,} ل.س\n"
                f"المطلوب: {total:,} ل.س\n"
                f"الناقص: {total - balance:,} ل.س",
                reply_markup=kb
            )
            return

        deduct_balance(user_id, total)

        state = user_states[user_id]
        state["step"] = "wait_admin_mtn_bill"
        summary = (
            f"🟡 طلب دفع فاتورة MTN:\n"
            f"👤 المستخدم: {user_id}\n"
            f"📱 {state['number']}\n"
            f"💵 {state['amount']:,} ل.س\n"
            f"🧾 مع العمولة: {total:,} ل.س"
        )
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=summary,
            payload={
                "type": "mtn_bill",
                "number": state["number"],
                "amount": state["amount"],
                "total": total,
                "reserved": total,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, "✅ تم إرسال طلبك للإدارة، بانتظار الموافقة.")
