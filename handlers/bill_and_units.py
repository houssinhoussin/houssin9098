# -*- coding: utf-8 -*-
# handlers/bill_and_units.py — وحدات/فواتير (سيرياتيل/MTN) مع HOLD ذري + رسائل موحّدة

from telebot import types
import math
import logging

from services.wallet_service import (
    register_user_if_not_exist,
    get_balance,               # لعرض الرصيد الحقيقي في رسالة الإدمن
    get_available_balance,     # ✅ المتاح = balance - held (شرط أساسي)
    create_hold,               # ✅ إنشاء حجز ذرّي
)

from services.queue_service import add_pending_request, process_queue
from services.telegram_safety import remove_inline_keyboard
from services.anti_spam import too_soon
from database.db import get_table  # موجود للتوافق لو احتجته

# ========== إعدادات عامة ==========
BAND = "━━━━━━━━━━━━━━━━"

def banner(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def _fmt_syp(n: int | float) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _user_name(call_or_msg) -> str:
    try:
        u = getattr(call_or_msg, "from_user", None) or getattr(call_or_msg, "chat", None)
        name = (getattr(u, "full_name", None) or getattr(u, "first_name", None) or "").strip()
        return name or "صديقنا"
    except Exception:
        return "صديقنا"

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def _unit_label(unit: dict) -> str:
    return f"{unit['name']} - {unit['price']:,} ل.س"

# ========== قوائم الوحدات ==========
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
PAGE_SIZE_UNITS = 5

# ========== كيبوردات رئيسية ==========
def units_bills_menu_inline():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔴 وحدات سيرياتيل", callback_data="ubm:syr_units"))
    kb.add(types.InlineKeyboardButton("🔴 فاتورة سيرياتيل", callback_data="ubm:syr_bill"))
    kb.add(types.InlineKeyboardButton("🟡 وحدات MTN", callback_data="ubm:mtn_units"))
    kb.add(types.InlineKeyboardButton("🟡 فاتورة MTN", callback_data="ubm:mtn_bill"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ubm:back"))
    return kb

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

# ========== تسجيل الهاندلرز ==========
def register_bill_and_units(bot, history):
    @bot.message_handler(func=lambda msg: msg.text == "💳 تحويل وحدات فاتورة سوري")
    def open_main_menu(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)
        history.setdefault(user_id, []).append("units_bills_menu")
        user_states[user_id] = {"step": None}
        bot.send_message(
            msg.chat.id,
            banner("🎛️ اختار الخدمة اللي تناسبك", ["جاهزين نزبطك بأحلى أسعار 😉"]),
            reply_markup=units_bills_menu_inline()
        )

    # راوتر القائمة الأساسية (Inline)
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ubm:"))
    def ubm_router(call):
        action = call.data.split(":", 1)[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "syr_units":
            user_states[user_id] = {"step": "select_syr_unit"}
            _send_syr_units_page(chat_id, page=0, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "syr_bill":
            user_states[user_id] = {"step": "syr_bill_number"}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 ادخل رقم سيرياتيل اللي هتدفع فاتورته:", chat_id, call.message.message_id, reply_markup=kb)
            return bot.answer_callback_query(call.id)

        if action == "mtn_units":
            user_states[user_id] = {"step": "select_mtn_unit"}
            _send_mtn_units_page(chat_id, page=0, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "mtn_bill":
            user_states[user_id] = {"step": "mtn_bill_number"}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 ادخل رقم MTN اللي هتدفع فاتورته:", chat_id, call.message.message_id, reply_markup=kb)
            return bot.answer_callback_query(call.id)

        if action == "back":
            bot.edit_message_text("⬅️ رجعناك للقائمة.", chat_id, call.message.message_id)
            bot.send_message(chat_id, "اختار من القائمة:", reply_markup=units_bills_menu_inline())
            return bot.answer_callback_query(call.id)

        bot.answer_callback_query(call.id)

    # ===== صفحات وحدات سيرياتيل/MTN =====
    def _send_syr_units_page(chat_id, page=0, message_id=None):
        items = [(idx, _unit_label(u)) for idx, u in enumerate(SYRIATEL_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="syrunits", back_data="ubm:back")
        txt = banner("🎯 اختار كمية الوحدات", [f"صفحة {page+1}/{pages}"])
        if message_id is not None:
            bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, txt, reply_markup=kb)

    def _send_mtn_units_page(chat_id, page=0, message_id=None):
        items = [(idx, _unit_label(u)) for idx, u in enumerate(MTN_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="mtnunits", back_data="ubm:back")
        txt = banner("🎯 اختار كمية الوحدات", [f"صفحة {page+1}/{pages}"])
        if message_id is not None:
            bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, txt, reply_markup=kb)

    # ===== كولباك وحدات سيرياتيل =====
    @bot.callback_query_handler(func=lambda call: call.data.startswith("syrunits:"))
    def syr_units_inline_handler(call):
        parts = call.data.split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "page":
            page = int(parts[2]) if len(parts) > 2 else 0
            _send_syr_units_page(chat_id, page=page, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "sel":
            idx = int(parts[2])
            unit = SYRIATEL_UNITS[idx]
            user_states[user_id] = {"step": "syr_unit_number", "unit": unit}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 ابعت الرقم/الكود اللي بيبدأ بـ 093 أو 098 أو 099:", chat_id, call.message.message_id, reply_markup=kb)
            return bot.answer_callback_query(call.id, text=_unit_label(unit))

        if action == "back":
            bot.edit_message_text("🎛️ اختار الخدمة:", chat_id, call.message.message_id, reply_markup=units_bills_menu_inline())
            return bot.answer_callback_query(call.id)

        bot.answer_callback_query(call.id)

    # ===== كولباك وحدات MTN =====
    @bot.callback_query_handler(func=lambda call: call.data.startswith("mtnunits:"))
    def mtn_units_inline_handler(call):
        parts = call.data.split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "page":
            page = int(parts[2]) if len(parts) > 2 else 0
            _send_mtn_units_page(chat_id, page=page, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "sel":
            idx = int(parts[2])
            unit = MTN_UNITS[idx]
            user_states[user_id] = {"step": "mtn_unit_number", "unit": unit}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text("📱 ابعت الرقم/الكود اللي بيبدأ بـ 094 أو 095 أو 096:", chat_id, call.message.message_id, reply_markup=kb)
            return bot.answer_callback_query(call.id, text=_unit_label(unit))

        if action == "back":
            bot.edit_message_text("🎛️ اختار الخدمة:", chat_id, call.message.message_id, reply_markup=units_bills_menu_inline())
            return bot.answer_callback_query(call.id)

        bot.answer_callback_query(call.id)

    # ===== إلغاء عام (زر cancel_all) =====
    @bot.callback_query_handler(func=lambda call: call.data == "cancel_all")
    def cancel_all_handler(call):
        user_states.pop(call.from_user.id, None)
        nm = _user_name(call)
        bot.edit_message_text(
            banner("❌ تم الإلغاء", [f"يا {nm}، رجعناك للقائمة."]),
            call.message.chat.id, call.message.message_id
        )
        try:
            bot.send_message(call.message.chat.id, "اختار من القائمة:", reply_markup=units_bills_menu_inline())
        except Exception:
            pass

    # ===================================================================
    #   (التوافق) مسارات الـ ReplyKeyboard القديمة — من غير حذف
    # ===================================================================

    ########## وحدات سيرياتيل (Reply) ##########
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
            return bot.send_message(msg.chat.id, "⚠️ اختار كمية من القائمة لو سمحت.")
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
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد الشراء", "syr_unit_final_confirm"))
        bot.send_message(
            msg.chat.id,
            banner("تأكيد العملية", [f"هنبعت {unit['name']} بسعر {_fmt_syp(unit['price'])} للرقم:", number]),
            reply_markup=kb
        )

    # تأكيد نهائي (HOLD) — سيرياتيل وحدات
    @bot.callback_query_handler(func=lambda call: call.data == "syr_unit_final_confirm")
    def syr_unit_final_confirm(call):
        user_id = call.from_user.id
        remove_inline_keyboard(bot, call.message)
        if too_soon(user_id, 'syr_unit_final_confirm', seconds=2):
            return bot.answer_callback_query(call.id, '⏱️ تم استلام طلبك..')
        user_id = call.from_user.id
        name = _user_name(call)

        state = user_states.get(user_id, {})
        unit = state.get("unit") or {}
        number = state.get("number")
        price = int(unit.get("price") or 0)
        unit_name = unit.get("name") or "وحدات سيرياتيل"

        available = get_available_balance(user_id)
        if available < price:
            missing = price - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(price)}", f"الناقص: {_fmt_syp(missing)}"]),
                reply_markup=kb
            )

        # إنشاء الحجز
        hold_id = None
        try:
            resp = create_hold(user_id, price, f"حجز وحدات سيرياتيل - {unit_name}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        # رسالة للإدمن
        bal_now = get_balance(user_id)
        admin_msg = (
            f"🧾 طلب وحدات سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم/الكود: <code>{number}</code>\n"
            f"🔖 الكمية: {unit_name}\n"
            f"💵 السعر: {price:,} ل.س\n"
            f"💼 رصيد المستخدم الآن: {bal_now:,} ل.س\n"
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
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["التنفيذ عادة من 1 إلى 4 دقايق وهيوصلك إشعار أول ما يتم."])
        )
        user_states[user_id]["step"] = "wait_admin_syr_unit"

    ########## وحدات MTN (Reply) ##########
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
            return bot.send_message(msg.chat.id, "⚠️ اختار كمية من القائمة لو سمحت.")
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
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد الشراء", "mtn_unit_final_confirm"))
        bot.send_message(
            msg.chat.id,
            banner("تأكيد العملية", [f"هنبعت {unit['name']} بسعر {_fmt_syp(unit['price'])} للرقم:", number]),
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "mtn_unit_final_confirm")
    def mtn_unit_final_confirm(call):
        user_id = call.from_user.id
        remove_inline_keyboard(bot, call.message)
        if too_soon(user_id, 'mtn_unit_final_confirm', seconds=2):
            return bot.answer_callback_query(call.id, '⏱️ تم استلام طلبك..')
        user_id = call.from_user.id
        name = _user_name(call)

        state = user_states.get(user_id, {})
        unit = state.get("unit") or {}
        number = state.get("number")
        price = int(unit.get("price") or 0)
        unit_name = unit.get("name") or "وحدات MTN"

        available = get_available_balance(user_id)
        if available < price:
            missing = price - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(price)}", f"الناقص: {_fmt_syp(missing)}"]),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, price, f"حجز وحدات MTN - {unit_name}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        bal_now = get_balance(user_id)
        admin_msg = (
            f"🧾 طلب وحدات MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم/الكود: <code>{number}</code>\n"
            f"🔖 الكمية: {unit_name}\n"
            f"💵 السعر: {price:,} ل.س\n"
            f"💼 رصيد المستخدم الآن: {bal_now:,} ل.س\n"
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
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["التنفيذ عادة من 1 إلى 4 دقايق وهيوصلك إشعار أول ما يتم."])
        )
        user_states[user_id]["step"] = "wait_admin_mtn_unit"

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
        bot.send_message(msg.chat.id, banner("تأكيد الرقم", [number]), reply_markup=kb)

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
        except Exception:
            return bot.send_message(msg.chat.id, "⚠️ ادخل مبلغ صحيح بالأرقام.")
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "syr_bill_amount_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_syr_bill_amount"), ("✔️ تأكيد", "confirm_syr_bill_amount"))
        bot.send_message(msg.chat.id, banner("تأكيد المبلغ", [f"المبلغ: {_fmt_syp(amount)}"]), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_amount")
    def edit_syr_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        bot.send_message(call.message.chat.id, "💵 ابعت مبلغ الفاتورة تاني:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_amount")
    def confirm_syr_bill_amount(call):
        user_id = call.from_user.id
        amount = user_states[user_id]["amount"]
        fee = int(round(amount * 0.10))
        amount_with_fee = amount + fee
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "syr_bill_final_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد", "final_confirm_syr_bill"))
        lines = [
            f"الرقم: {user_states[user_id]['number']}",
            f"المبلغ: {_fmt_syp(amount)}",
            f"أجور التحويل: {_fmt_syp(fee)}",
            f"الإجمالي: {_fmt_syp(amount_with_fee)}",
            "نكمّل؟"
        ]
        bot.send_message(call.message.chat.id, banner("تفاصيل الفاتورة (سيرياتيل)", lines), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_syr_bill")
    def final_confirm_syr_bill(call):
        user_id = call.from_user.id
        remove_inline_keyboard(bot, call.message)
        if too_soon(user_id, 'final_confirm_syr_bill', seconds=2):
            return bot.answer_callback_query(call.id, '⏱️ تم استلام طلبك..')
        user_id = call.from_user.id
        name = _user_name(call)

        state = user_states.get(user_id, {})
        number = state.get("number")
        amount = int(state.get("amount") or 0)
        total  = int(state.get("amount_with_fee") or amount)

        available = get_available_balance(user_id)
        if available < total:
            missing = total - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(total)}", f"الناقص: {_fmt_syp(missing)}"]),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, total, f"حجز فاتورة سيرياتيل للرقم {number}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        bal_now = get_balance(user_id)
        admin_msg = (
            f"🧾 دفع فاتورة سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم: <code>{number}</code>\n"
            f"💵 المبلغ: {amount:,} ل.س\n"
            f"🧾 الإجمالي مع العمولة: {total:,} ل.س\n"
            f"💼 رصيد المستخدم الآن: {bal_now:,} ل.س\n"
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
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["التنفيذ عادة من 1 إلى 4 دقايق وهيوصلك إشعار أول ما يتم."])
        )
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
        bot.send_message(msg.chat.id, banner("تأكيد الرقم", [number]), reply_markup=kb)

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
        except Exception:
            return bot.send_message(msg.chat.id, "⚠️ ادخل مبلغ صحيح بالأرقام.")
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "mtn_bill_amount_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_mtn_bill_amount"), ("✔️ تأكيد", "confirm_mtn_bill_amount"))
        bot.send_message(msg.chat.id, banner("تأكيد المبلغ", [f"المبلغ: {_fmt_syp(amount)}"]), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_amount")
    def edit_mtn_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        bot.send_message(call.message.chat.id, "💵 ابعت مبلغ الفاتورة تاني:")

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_amount")
    def confirm_mtn_bill_amount(call):
        user_id = call.from_user.id
        amount = user_states[user_id]["amount"]
        fee = int(round(amount * 0.10))
        amount_with_fee = amount + fee
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "mtn_bill_final_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد", "final_confirm_mtn_bill"))
        lines = [
            f"الرقم: {user_states[user_id]['number']}",
            f"المبلغ: {_fmt_syp(amount)}",
            f"أجور التحويل: {_fmt_syp(fee)}",
            f"الإجمالي: {_fmt_syp(amount_with_fee)}",
            "نكمّل؟"
        ]
        bot.send_message(call.message.chat.id, banner("تفاصيل الفاتورة (MTN)", lines), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_mtn_bill")
    def final_confirm_mtn_bill(call):
        user_id = call.from_user.id
        remove_inline_keyboard(bot, call.message)
        if too_soon(user_id, 'final_confirm_mtn_bill', seconds=2):
            return bot.answer_callback_query(call.id, '⏱️ تم استلام طلبك..')
        user_id = call.from_user.id
        name = _user_name(call)

        state = user_states.get(user_id, {})
        number = state.get("number")
        amount = int(state.get("amount") or 0)
        total  = int(state.get("amount_with_fee") or amount)

        available = get_available_balance(user_id)
        if available < total:
            missing = total - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(total)}", f"الناقص: {_fmt_syp(missing)}"]),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, total, f"حجز فاتورة MTN للرقم {number}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.")

        bal_now = get_balance(user_id)
        admin_msg = (
            f"🧾 دفع فاتورة MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم: <code>{number}</code>\n"
            f"💵 المبلغ: {amount:,} ل.س\n"
            f"🧾 الإجمالي مع العمولة: {total:,} ل.س\n"
            f"💼 رصيد المستخدم الآن: {bal_now:,} ل.س\n"
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
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["التنفيذ عادة من 1 إلى 4 دقايق وهيوصلك إشعار أول ما يتم."])
        )
        user_states[user_id]["step"] = "wait_admin_mtn_bill"

# واجهة يستدعيها main.py
def register(bot):
    register_bill_and_units(bot, {})
