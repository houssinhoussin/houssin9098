# handlers/bill_and_units.py
from telebot import types
import math  # added for pagination support
import logging
from services.wallet_service import (
    get_balance,
    deduct_balance,            # يُترك للتوافق لو فيه استخدام قديم داخلي
    add_balance,               # يُترك للتوافق
    register_user_if_not_exist,
    add_purchase,              # يُترك للتوافق
    has_sufficient_balance,    # يُترك للتوافق
    create_hold,               # ✅ الحجز
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

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _user_name(call_or_msg) -> str:
    try:
        u = getattr(call_or_msg, "from_user", None) or getattr(call_or_msg, "chat", None)
        name = getattr(u, "full_name", None) or getattr(u, "first_name", None) or ""
        name = (name or "").strip()
        return name if name else "صديقنا"
    except Exception:
        return "صديقنا"

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

# =======================================================================
# التسجيل الرئيسي
# =======================================================================
def register_bill_and_units(bot, history):
    """تسجيل جميع هاندلرات خدمات (وحدات/فواتير) لكل من سيرياتيل و MTN.
    تم إضافة دعم InlineKeyboard مع Pagination دون المساس بمنطق المراحل الحالي.
    كل الهاندلرات الأصلية (القائمة على ReplyKeyboard) باقية كما هي للتوافق.
    """

    # ===== القائمة الرئيسية للخدمة =====
    @bot.message_handler(func=lambda msg: msg.text == "💳 تحويل وحدات فاتورة سوري")
    def open_main_menu(msg):
        user_id = msg.from_user.id
        history.setdefault(user_id, []).append("units_bills_menu")
        user_states[user_id] = {"step": None}
        bot.send_message(msg.chat.id, "🎛️ اختار الخدمة اللي عايزها يا باشا:", reply_markup=units_bills_menu_inline())

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
            bot.edit_message_text("📱 ادخل رقم سيرياتيل اللي هتدفع فاتورته:", chat_id, call.message.message_id, reply_markup=kb)
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
            bot.edit_message_text("📱 ادخل رقم MTN اللي هتدفع فاتورته:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        if action == "back":
            try:
                from keyboards import main_menu as _main_menu
                bot.edit_message_text("⬅️ رجعناك للقائمة.", chat_id, call.message.message_id)
                bot.send_message(chat_id, "اختار من القائمة:", reply_markup=_main_menu())
            except Exception:
                bot.edit_message_text("⬅️ رجعناك للقائمة.", chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    # ---------- أدوات إرسال قوائم الوحدات (Inline + Pagination) ----------
    PAGE_SIZE_UNITS = 5

    def _send_syr_units_page(chat_id, page=0, message_id=None):
        items = [(idx, _unit_label(u)) for idx, u in enumerate(SYRIATEL_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="syrunits", back_data="ubm:back")
        text = f"🎯 اختار كمية الوحدات (صفحة {page+1}/{pages}):"
        if message_id is not None:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)

    def _send_mtn_units_page(chat_id, page=0, message_id=None):
        items = [(idx, _unit_label(u)) for idx, u in enumerate(MTN_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="mtnunits", back_data="ubm:back")
        text = f"🎯 اختار كمية الوحدات (صفحة {page+1}/{pages}):"
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
            bot.edit_message_text("📱 ابعت الرقم/الكود اللي بيبدأ بـ 093 أو 098 أو 099:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id, text=_unit_label(unit))
            return

        if action == "back":
            bot.edit_message_text("🎛️ اختار الخدمة:", chat_id, call.message.message_id, reply_markup=units_bills_menu_inline())
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
            bot.edit_message_text("📱 ابعت الرقم/الكود اللي بيبدأ بـ 094 أو 095 أو 096:", chat_id, call.message.message_id, reply_markup=kb)
            bot.answer_callback_query(call.id, text=_unit_label(unit))
            return

        if action == "back":
            bot.edit_message_text("🎛️ اختار الخدمة:", chat_id, call.message.message_id, reply_markup=units_bills_menu_inline())
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)

    # ===================================================================
    # أدناه الكود الأصلي للمعالجة بالرسائل (ReplyKeyboard) بدون أي تعديل
    # ===================================================================

    ########## وحدات سيرياتيل ##########
    @bot.message_handler(func=lambda msg: msg.text == "🔴 وحدات سيرياتيل")
    def syr_units_menu(msg):
        user_id = msg.from_user.id
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for u in SYRIATEL_UNITS:
            kb.add(types.KeyboardButton(_unit_label(u)))
        kb.add(types.KeyboardButton("⬅️ رجوع"))
        user_states[user_id] = {"step": "select_syr_unit"}
        bot.send_message(msg.chat.id, "🎯 اختار كمية الوحدات:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "select_syr_unit")
    def syr_unit_select(msg):
        user_id = msg.from_user.id
        unit = next((u for u in SYRIATEL_UNITS if _unit_label(u) == msg.text), None)
        if not unit:
            bot.send_message(msg.chat.id, "⚠️ اختار كمية من القائمة لو سمحت.")
            return
        user_states[user_id] = {"step": "syr_unit_number", "unit": unit}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 ابعت الرقم/الكود اللي بيبدأ بـ 093 أو 098 أو 099:", reply_markup=kb)

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
            f"تأكيد يا باشا؟ هنبعت {unit['name']} بسعر {unit['price']:,} ل.س للرقم:\n{number}",
            reply_markup=kb
        )

    # === بدل الخصم ➜ حجز بمبلغ العملية (Hold) وإرسال للطابور ===
    @bot.callback_query_handler(func=lambda call: call.data == "syr_unit_final_confirm")
    def syr_unit_final_confirm(call):
        user_id = call.from_user.id
        name = _user_name(call)

        # منع الطلب المتزامن
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            return bot.send_message(call.message.chat.id, f"⌛ يا {name}، عندك طلب قيد المراجعة. استنى ثواني لحد ما نخلصه.")

        state = user_states.get(user_id, {})
        unit = state.get("unit") or {}
        number = state.get("number")
        price = int(unit.get("price") or 0)
        unit_name  = unit.get("name") or "وحدات سيرياتيل"

        # التحقق من الرصيد ثم الحجز
        balance = get_balance(user_id)
        if balance < price:
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("💼 الذهاب للمحفظة", "go_wallet"))
            missing = price - (balance or 0)
            bot.send_message(
                call.message.chat.id,
                f"❌ يا {name}، رصيدك مش مكفّي.\nرصيدك: {_fmt_syp(balance)}\nالمطلوب: {_fmt_syp(price)}\nالناقص: {_fmt_syp(missing)}",
                reply_markup=kb
            )
            user_states.pop(user_id, None)
            return

        # ✅ حجز
        hold_id = None
        try:
            res = create_hold(user_id, price, f"حجز وحدات سيرياتيل - {unit_name}")
            hold_id = (res.data[0]["id"] if getattr(res, "data", None) else res.get("id"))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        # رسالة الأدمن الموحّدة + تمرير hold_id
        admin_msg = (
            f"💰 رصيد المستخدم: {_fmt_syp(balance)}\n"
            f"🆕 طلب وحدات سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم/الكود: <code>{number}</code>\n"
            f"🔖 الكمية: {unit_name}\n"
            f"💵 السعر: {_fmt_syp(price)}\n"
            f"(type=syr_unit)"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "syr_unit",
                "number": number,
                "unit_name": unit_name,
                "price": price,
                "reserved": price,
                "hold_id": hold_id,        # ✅ مهم
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, f"✅ تمام يا {name}! اتطلبك وصل للإدارة. التنفيذ بياخد من 1 لـ 4 دقايق، وهبلغك أول ما يخلص 😉")
        user_states[user_id]["step"] = "wait_admin_syr_unit"

    def cancel_all(call):
        user_states.pop(call.from_user.id, None)
        bot.edit_message_text("❌ تم إلغاء العملية.", call.message.chat.id, call.message.message_id)

    ########## وحدات MTN ##########
    @bot.message_handler(func=lambda msg: msg.text == "🟡 وحدات MTN")
    def mtn_units_menu(msg):
        user_id = msg.from_user.id
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for u in MTN_UNITS:
            kb.add(types.KeyboardButton(_unit_label(u)))
        kb.add(types.KeyboardButton("⬅️ رجوع"))
        user_states[user_id] = {"step": "select_mtn_unit"}
        bot.send_message(msg.chat.id, "🎯 اختار كمية الوحدات:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "select_mtn_unit")
    def mtn_unit_select(msg):
        user_id = msg.from_user.id
        unit = next((u for u in MTN_UNITS if _unit_label(u) == msg.text), None)
        if not unit:
            bot.send_message(msg.chat.id, "⚠️ اختار كمية من القائمة لو سمحت.")
            return
        user_states[user_id] = {"step": "mtn_unit_number", "unit": unit}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 ابعت الرقم/الكود اللي بيبدأ بـ 094 أو 095 أو 096:", reply_markup=kb)

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
            f"تأكيد يا باشا؟ هنبعت {unit['name']} بسعر {unit['price']:,} ل.س للرقم:\n{number}",
            reply_markup=kb
        )

    # === بدل الخصم ➜ حجز بمبلغ العملية (Hold) وإرسال للطابور ===
    @bot.callback_query_handler(func=lambda call: call.data == "mtn_unit_final_confirm")
    def mtn_unit_final_confirm(call):
        user_id = call.from_user.id
        name = _user_name(call)

        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            return bot.send_message(call.message.chat.id, f"⌛ يا {name}، عندك طلب قيد المراجعة. استنى ثواني لحد ما نخلصه.")

        state = user_states.get(user_id, {})
        unit = state.get("unit") or {}
        number = state.get("number")
        price = int(unit.get("price") or 0)
        unit_name  = unit.get("name") or "وحدات MTN"

        balance = get_balance(user_id)
        if balance < price:
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("💼 الذهاب للمحفظة", "go_wallet"))
            missing = price - (balance or 0)
            bot.send_message(
                call.message.chat.id,
                f"❌ يا {name}، رصيدك مش مكفّي.\nرصيدك: {_fmt_syp(balance)}\nالمطلوب: {_fmt_syp(price)}\nالناقص: {_fmt_syp(missing)}",
                reply_markup=kb
            )
            user_states.pop(user_id, None)
            return

        hold_id = None
        try:
            res = create_hold(user_id, price, f"حجز وحدات MTN - {unit_name}")
            hold_id = (res.data[0]["id"] if getattr(res, "data", None) else res.get("id"))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        admin_msg = (
            f"💰 رصيد المستخدم: {_fmt_syp(balance)}\n"
            f"🆕 طلب وحدات MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم/الكود: <code>{number}</code>\n"
            f"🔖 الكمية: {unit_name}\n"
            f"💵 السعر: {_fmt_syp(price)}\n"
            f"(type=mtn_unit)"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "mtn_unit",
                "number": number,
                "unit_name": unit_name,
                "price": price,
                "reserved": price,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, f"✅ تمام يا {name}! اتطلبك وصل للإدارة. التنفيذ بياخد من 1 لـ 4 دقايق، وهبلغك أول ما يخلص 😉")
        user_states[user_id]["step"] = "wait_admin_mtn_unit"

    # ========== (الهاندلرات الإجرائية القديمة تُترك للتوافق إن استُدعيت يدويًا) ==========
    def admin_accept_mtn_unit(call):
        user_id = int(call.data.split("_")[-1])
        state = user_states.get(user_id, {})
        price = state.get("unit", {}).get("price", 0)
        balance = get_balance(user_id)
        if balance < price:
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("💼 الذهاب للمحفظة", "go_wallet"))
            bot.send_message(user_id, f"❌ لا يوجد رصيد كافٍ.\nرصيدك: {_fmt_syp(balance)}\nالمطلوب: {_fmt_syp(price)}\nالناقص: {_fmt_syp(price-balance)}", reply_markup=kb)
            bot.answer_callback_query(call.id, "❌ رصيد غير كافٍ")
            user_states.pop(user_id, None)
            return
        deduct_balance(user_id, price)
        bot.send_message(user_id, f"✅ تم شراء {state['unit']['name']} لوحدات MTN بنجاح.")
        bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
        user_states.pop(user_id, None)

    ########## فاتورة سيرياتيل ##########
    @bot.message_handler(func=lambda msg: msg.text == "🔴 فاتورة سيرياتيل")
    def syr_bill_entry(msg):
        user_id = msg.from_user.id
        user_states[user_id] = {"step": "syr_bill_number"}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 ابعت رقم سيرياتيل اللي هتدفع فاتورته:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_bill_number")
    def syr_bill_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        user_states[user_id]["number"] = number
        user_states[user_id]["step"] = "syr_bill_number_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_syr_bill_number"), ("✔️ تأكيد", "confirm_syr_bill_number"))
        bot.send_message(msg.chat.id, f"تأكيد الرقم ده؟\n{number}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_number")
    def edit_syr_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_number"
        bot.send_message(call.message.chat.id, "📱 ابعت رقم الموبايل تاني:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_number")
    def confirm_syr_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(call.message.chat.id, "💵 ابعت مبلغ الفاتورة بالليرة:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_bill_amount")
    def syr_bill_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except:
            bot.send_message(msg.chat.id, "⚠️ ادخل مبلغ صحيح بالأرقام.")
            return
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "syr_bill_amount_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_syr_bill_amount"), ("✔️ تأكيد", "confirm_syr_bill_amount"))
        bot.send_message(msg.chat.id, f"تمام؟ المبلغ: {_fmt_syp(amount)}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_amount")
    def edit_syr_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        bot.send_message(call.message.chat.id, "💵 ابعت مبلغ الفاتورة تاني:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_amount")
    def confirm_syr_bill_amount(call):
        user_id = call.from_user.id
        amount = user_states[user_id]["amount"]
        amount_with_fee = int(amount * 1.10)
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "syr_bill_final_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد", "final_confirm_syr_bill"))
        bot.send_message(
            call.message.chat.id,
            f"هيتدفع فاتورة سيرياتيل للرقم: {user_states[user_id]['number']}\n"
            f"المبلغ: {_fmt_syp(amount)}\n"
            f"أجور التحويل: {_fmt_syp(amount_with_fee-amount)}\n"
            f"الإجمالي: {_fmt_syp(amount_with_fee)}\n"
            "نكمّل؟",
            reply_markup=kb
        )

    # === بدل الخصم ➜ حجز بمبلغ الفاتورة (Hold) وإرسال للطابور ===
    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_syr_bill")
    def final_confirm_syr_bill(call):
        user_id = call.from_user.id
        name = _user_name(call)

        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            return bot.send_message(call.message.chat.id, f"⌛ يا {name}، عندك طلب قيد المراجعة. استنى ثواني لحد ما نخلصه.")

        state = user_states.get(user_id, {})
        number = state.get("number")
        amount = int(state.get("amount") or 0)
        total  = int(state.get("amount_with_fee") or amount)

        balance = get_balance(user_id)
        if balance < total:
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("💼 المحفظة", "go_wallet"))
            bot.send_message(
                call.message.chat.id,
                f"❌ يا {name}، رصيدك مش مكفّي.\nرصيدك: {_fmt_syp(balance)}\nالمطلوب: {_fmt_syp(total)}\nالناقص: {_fmt_syp(total-balance)}",
                reply_markup=kb
            )
            user_states.pop(user_id, None)
            return

        hold_id = None
        try:
            res = create_hold(user_id, total, f"حجز فاتورة سيرياتيل للرقم {number}")
            hold_id = (res.data[0]["id"] if getattr(res, "data", None) else res.get("id"))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        admin_msg = (
            f"💰 رصيد المستخدم: {_fmt_syp(balance)}\n"
            f"🆕 دفع فاتورة سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم: <code>{number}</code>\n"
            f"💵 المبلغ: {_fmt_syp(amount)}\n"
            f"🧾 الإجمالي مع العمولة: {_fmt_syp(total)}\n"
            f"(type=syr_bill)"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "syr_bill",
                "number": number,
                "amount": amount,
                "total": total,
                "reserved": total,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, f"✅ تمام يا {name}! اتطلبك وصل للإدارة. التنفيذ بياخد من 1 لـ 4 دقايق، وهبلغك أول ما يخلص 😉")
        user_states[user_id]["step"] = "wait_admin_syr_bill"

    ########## فاتورة MTN ##########
    @bot.message_handler(func=lambda msg: msg.text == "🟡 فاتورة MTN")
    def mtn_bill_entry(msg):
        user_id = msg.from_user.id
        user_states[user_id] = {"step": "mtn_bill_number"}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, "📱 ابعت رقم MTN اللي هتدفع فاتورته:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_bill_number")
    def mtn_bill_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        user_states[user_id]["number"] = number
        user_states[user_id]["step"] = "mtn_bill_number_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_mtn_bill_number"), ("✔️ تأكيد", "confirm_mtn_bill_number"))
        bot.send_message(msg.chat.id, f"تأكيد الرقم ده؟\n{number}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_number")
    def edit_mtn_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_number"
        bot.send_message(call.message.chat.id, "📱 ابعت رقم الموبايل تاني:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_number")
    def confirm_mtn_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(call.message.chat.id, "💵 ابعت مبلغ الفاتورة بالليرة:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_bill_amount")
    def mtn_bill_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except:
            bot.send_message(msg.chat.id, "⚠️ ادخل مبلغ صحيح بالأرقام.")
            return
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "mtn_bill_amount_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_mtn_bill_amount"), ("✔️ تأكيد", "confirm_mtn_bill_amount"))
        bot.send_message(msg.chat.id, f"تمام؟ المبلغ: {_fmt_syp(amount)}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_amount")
    def edit_mtn_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        bot.send_message(call.message.chat.id, "💵 ابعت مبلغ الفاتورة تاني:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_amount")
    def confirm_mtn_bill_amount(call):
        user_id = call.from_user.id
        amount = user_states[user_id]["amount"]
        amount_with_fee = int(amount * 1.10)
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "mtn_bill_final_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد", "final_confirm_mtn_bill"))
        bot.send_message(
            call.message.chat.id,
            f"هيتدفع فاتورة MTN للرقم: {user_states[user_id]['number']}\n"
            f"المبلغ: {_fmt_syp(amount)}\n"
            f"أجور التحويل: {_fmt_syp(amount_with_fee-amount)}\n"
            f"الإجمالي: {_fmt_syp(amount_with_fee)}\n"
            "نكمّل؟",
            reply_markup=kb
        )

    # === بدل الخصم ➜ حجز بمبلغ الفاتورة (Hold) وإرسال للطابور ===
    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_mtn_bill")
    def final_confirm_mtn_bill(call):
        user_id = call.from_user.id
        name = _user_name(call)

        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            return bot.send_message(call.message.chat.id, f"⌛ يا {name}، عندك طلب قيد المراجعة. استنى ثواني لحد ما نخلصه.")

        state = user_states.get(user_id, {})
        number = state.get("number")
        amount = int(state.get("amount") or 0)
        total  = int(state.get("amount_with_fee") or amount)

        balance = get_balance(user_id)
        if balance < total:
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("💼 المحفظة", "go_wallet"))
            bot.send_message(
                call.message.chat.id,
                f"❌ يا {name}، رصيدك مش مكفّي.\nرصيدك: {_fmt_syp(balance)}\nالمطلوب: {_fmt_syp(total)}\nالناقص: {_fmt_syp(total-balance)}",
                reply_markup=kb
            )
            user_states.pop(user_id, None)
            return

        hold_id = None
        try:
            res = create_hold(user_id, total, f"حجز فاتورة MTN للرقم {number}")
            hold_id = (res.data[0]["id"] if getattr(res, "data", None) else res.get("id"))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        admin_msg = (
            f"💰 رصيد المستخدم: {_fmt_syp(balance)}\n"
            f"🆕 دفع فاتورة MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم: <code>{number}</code>\n"
            f"💵 المبلغ: {_fmt_syp(amount)}\n"
            f"🧾 الإجمالي مع العمولة: {_fmt_syp(total)}\n"
            f"(type=mtn_bill)"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "mtn_bill",
                "number": number,
                "amount": amount,
                "total": total,
                "reserved": total,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(call.message.chat.id, f"✅ تمام يا {name}! اتطلبك وصل للإدارة. التنفيذ بياخد من 1 لـ 4 دقايق، وهبلغك أول ما يخلص 😉")
        user_states[user_id]["step"] = "wait_admin_mtn_bill"

    # زر الذهاب للمحفظة في حال الرصيد غير كافٍ
    @bot.callback_query_handler(func=lambda call: call.data == "go_wallet")
    def go_wallet(call):
        user_states.pop(call.from_user.id, None)
        bot.send_message(call.message.chat.id, "💼 علشان تروح للمحفظة، اختار زر «محفظتي» من القائمة الرئيسية.") 

def register(bot):
    """
    تستدعى من main.py لتسجيل جميع الهاندلرات في هذا الملف
    """
    register_bill_and_units(bot, {})
