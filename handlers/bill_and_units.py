# -*- coding: utf-8 -*-
# handlers/bill_and_units.py — وحدات/فواتير (سيرياتيل/MTN) مع HOLD ذري + رسائل موحّدة + /cancel

from telebot import types
import math
import logging

from services.wallet_service import (
    register_user_if_not_exist,
    get_balance,               # لعرض الرصيد الحقيقي في رسالة الإدمن
    get_available_balance,     # ✅ المتاح = balance - held (شرط أساسي)
    create_hold,               # ✅ إنشاء حجز ذرّي
)

try:
    from services.queue_service import add_pending_request, process_queue
except Exception:
    def add_pending_request(*args, **kwargs):
        return None
    def process_queue(*args, **kwargs):
        return None

from services.telegram_safety import remove_inline_keyboard
from services.anti_spam import too_soon
from services.ui_guards import confirm_guard  # ✅ حارس التأكيد الموحّد
from database.db import get_table  # موجود للتوافق لو احتجته

# جديد: فحص الصيانة + أعلام المزايا (Feature Flags)
from services.system_service import is_maintenance, maintenance_message
from services.feature_flags import block_if_disabled, is_feature_enabled
from services.feature_flags import slugify

# ===== (جديد) خصومات للوحدات والفواتير فقط — استثناء الكازية تمامًا =====
try:
    from services.discount_service import apply_discount
    from services.referral_service import revalidate_user_discount
except Exception:
    # أمان: لو لم تتوفر الخدمات، نعرّف بدائل محايدة بلا خصم
    def apply_discount(user_id: int, amount: int):
        return int(amount), None
    def revalidate_user_discount(bot, user_id: int):
        return None

# ========== إعدادات عامة ==========
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."

def banner(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def with_cancel_hint(text: str) -> str:
    # يضيف سطر /cancel لمحتوى الرسالة
    return f"{text}\n\n{CANCEL_HINT}"

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
    
def _kz_label(item: dict) -> str:
    # يظهر "المبلغ • السعر"
    return f"{int(item['amount']):,} ل.س • {int(item['price']):,} ل.س"

def key_kazia(carrier: str, amount: int | float) -> str:
    # مفتاح ميزة لكل مبلغ كازية
    return f"kazia:{slugify(carrier)}:{int(amount)}"

def _unit_label(unit: dict) -> str:
    return f"{unit['name']} • {unit['price']:,} ل.س"

def _lamp(key: str) -> str:
    return "🟢" if is_feature_enabled(key, True) else "🔴"

# ========== (جديد) تحكّم تفصيلي لكل كمية وحدات ==========
# نستخدم نفس جدول features مع مفاتيح ديناميكية مثل:
# units:syriatel:3068-وحدة  |  units:mtn:10000-وحدة
_FEATURES_TABLE = "features"

def _features_tbl():
    return get_table(_FEATURES_TABLE)

def key_units(carrier: str, unit_name: str) -> str:
    return f"units:{slugify(carrier)}:{slugify(unit_name)}"

def ensure_feature(key: str, label: str, default_active: bool = True) -> None:
    """يزرع سطر في features إن لم يوجد (idempotent)."""
    try:
        r = _features_tbl().select("key").eq("key", key).limit(1).execute()
        if not getattr(r, "data", None):
            _features_tbl().insert({"key": key, "label": label, "active": bool(default_active)}).execute()
        else:
            # تحدّث الملصق لو تغيّر
            _features_tbl().update({"label": label}).eq("key", key).execute()
    except Exception as e:
        logging.exception("[bill_and_units] ensure_feature failed: %s", e)

def require_feature_or_alert(bot, chat_id: int, key: str, label: str) -> bool:
    """
    إن كانت الميزة مقفلة يرجّع True بعد إرسال اعتذار أنيق للعميل.
    وإلا يرجّع False ويُسمح بالمتابعة.
    """
    if is_feature_enabled(key, True):
        return False
    try:
        bot.send_message(
            chat_id,
            with_cancel_hint(
                f"⛔ عذرًا، «{label}» غير متاح حاليًا (نفاد الكمية/صيانة).\n"
                f"نعمل على إعادته بأسرع وقت. شكرًا لتفهمك 🤍"
            )
        )
    except Exception:
        pass
    return True

# ========== قوائم الوحدات ==========
SYRIATEL_UNITS = [
    {"name": "1000 وحدة", "price": 1125},
    {"name": "2013 وحدة", "price": 2265},
    {"name": "4026 وحدة", "price": 4528},
    {"name": "5273 وحدة", "price": 5976},
    {"name": "7766 وحدة", "price": 8733},
    {"name": "9587 وحدة", "price": 10780},
    {"name": "14381 وحدة", "price": 16170},
    {"name": "16011 وحدة", "price": 18002},
    {"name": "18312 وحدة", "price": 20588},
    {"name": "23969 وحدة", "price": 26950},
    {"name": "36912 وحدة", "price": 41500},
    {"name": "47938 وحدة", "price": 53896},
    {"name": "57526 وحدة", "price": 64675},
    {"name": "62320 وحدة", "price": 70066},
    {"name": "71907 وحدة", "price": 80842},
    {"name": "94918 وحدة", "price": 106715             },
]

MTN_UNITS = [
    {"name": "1000 وحدة", "price": 1125},
    {"name": "5000 وحدة", "price": 5625},
    {"name": "8500 وحدة", "price": 9557},
    {"name": "10000 وحدة", "price": 11242},
    {"name": "15000 وحدة", "price": 16865},
    {"name": "20000 وحدة", "price": 22485},
    {"name": "30000 وحدة", "price": 33728},
    {"name": "50000 وحدة", "price": 56215},
    {"name": "100000 وحدة", "price": 112425},
]
# ========== (جديد) مبالغ جملة (كازية) ==========
KAZIA_OPTIONS_SYR = [
    {"amount":  50000,  "price":   53500},
    {"amount": 100000,  "price":  107000},
    {"amount": 150000,  "price":  160500},
    {"amount": 200000,  "price":  214000},
    {"amount": 250000,  "price":  267500},
    {"amount": 300000,  "price":  321000},
    {"amount": 400000,  "price":  428000},
    {"amount": 500000,  "price":  530000},
    {"amount":1000000,  "price": 1070000},
]

# نفس الأسعار لـ MTN حسب طلبك
KAZIA_OPTIONS_MTN = [
    {"amount":  50000,  "price":   53500},
    {"amount": 100000,  "price":  107000},
    {"amount": 150000,  "price":  160500},
    {"amount": 200000,  "price":  214000},
    {"amount": 250000,  "price":  267500},
    {"amount": 300000,  "price":  321000},
    {"amount": 400000,  "price":  428000},
    {"amount": 500000,  "price":  530000},
    {"amount":1000000,  "price": 1070000},
]

from services.state_adapter import UserStateDictLike
user_states = UserStateDictLike()
PAGE_SIZE_UNITS = 5

# ========== كيبوردات رئيسية ==========
def units_bills_menu_inline():
    """قائمة إنلاين ديناميكية تُظهر حالة المزايا (🟢/🔴)."""
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"{_lamp('syr_unit')} وحدات سيرياتيل", callback_data="ubm:syr_units"))
    kb.add(types.InlineKeyboardButton(f"{_lamp('syr_bill')} فاتورة سيرياتيل", callback_data="ubm:syr_bill"))
    kb.add(types.InlineKeyboardButton(f"{_lamp('mtn_unit')} وحدات MTN", callback_data="ubm:mtn_units"))
    kb.add(types.InlineKeyboardButton(f"{_lamp('mtn_bill')} فاتورة MTN", callback_data="ubm:mtn_bill"))
    kb.add(types.InlineKeyboardButton(f"{_lamp('syr_kazia')} جملة (كازية) سيرياتيل", callback_data="ubm:syr_kazia"))
    kb.add(types.InlineKeyboardButton(f"{_lamp('mtn_kazia')} جملة (كازية) MTN", callback_data="ubm:mtn_kazia"))
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

# ========== /cancel — إلغاء عام ==========
def _reset_state(user_id: int):
    user_states.pop(user_id, None)

def register_bill_and_units(bot, history):
    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(msg):
        uid = msg.from_user.id
        name = _user_name(msg)
        _reset_state(uid)
        bot.send_message(
            msg.chat.id,
            banner("❌ تم الإلغاء", [f"يا {name}، رجّعناك للقائمة 👇", "اختار اللي يناسبك وهنخلّصهولك بسرعة 😉"]),
            reply_markup=units_bills_menu_inline()
        )

    @bot.message_handler(func=lambda msg: msg.text == "💳 تحويل وحدات فاتورة سوري")
    def open_main_menu(msg):
        # ✅ إنهاء أي رحلة/مسار سابق عالق
        try:
            from handlers.start import _reset_user_flows
            _reset_user_flows(msg.from_user.id)
        except Exception:
            pass

        # صيانة؟
        if is_maintenance():
            return bot.send_message(msg.chat.id, maintenance_message())
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)
        history.setdefault(user_id, []).append("units_bills_menu")
        user_states[user_id] = {"step": None}
        bot.send_message(
            msg.chat.id,
            with_cancel_hint(banner("🎛️ اختار الخدمة اللي تناسبك", ["جاهزين نزبطك بأحلى أسعار 😉"])),
            reply_markup=units_bills_menu_inline()
        )

    # راوتر القائمة الأساسية (Inline)
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ubm:"))
    def ubm_router(call):
        # صيانة؟
        if is_maintenance():
            bot.answer_callback_query(call.id)
            return bot.send_message(call.message.chat.id, maintenance_message())

        action = call.data.split(":", 1)[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "syr_units":
            # ميزة مفعّلة؟
            if block_if_disabled(bot, chat_id, "syr_unit", "وحدات سيرياتيل"):
                return bot.answer_callback_query(call.id)
            user_states[user_id] = {"step": "select_syr_unit"}
            _send_syr_units_page(chat_id, page=0, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "syr_bill":
            if block_if_disabled(bot, chat_id, "syr_bill", "فاتورة سيرياتيل"):
                return bot.answer_callback_query(call.id)
            user_states[user_id] = {"step": "syr_bill_number"}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text(
                with_cancel_hint("📱 ابعت رقم سيرياتيل اللي هتدفع فاتورته:"),
                chat_id, call.message.message_id, reply_markup=kb
            )
            return bot.answer_callback_query(call.id)

        if action == "mtn_units":
            if block_if_disabled(bot, chat_id, "mtn_unit", "وحدات MTN"):
                return bot.answer_callback_query(call.id)
            user_states[user_id] = {"step": "select_mtn_unit"}
            _send_mtn_units_page(chat_id, page=0, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "mtn_bill":
            if block_if_disabled(bot, chat_id, "mtn_bill", "فاتورة MTN"):
                return bot.answer_callback_query(call.id)
            user_states[user_id] = {"step": "mtn_bill_number"}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text(
                with_cancel_hint("📱 ابعت رقم MTN اللي هتدفع فاتورته:"),
                chat_id, call.message.message_id, reply_markup=kb
            )
            return bot.answer_callback_query(call.id)
            
        if action == "syr_kazia":
            if block_if_disabled(bot, chat_id, "syr_kazia", "جملة (كازية) سيرياتيل"):
                return bot.answer_callback_query(call.id)
            user_states[user_id] = {"step": "select_syr_kazia"}
            _send_syr_kazia_page(chat_id, page=0, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "mtn_kazia":
            if block_if_disabled(bot, chat_id, "mtn_kazia", "جملة (كازية) MTN"):
                return bot.answer_callback_query(call.id)
            user_states[user_id] = {"step": "select_mtn_kazia"}
            _send_mtn_kazia_page(chat_id, page=0, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "back":
            # ✅ صفّر الحالة قبل الرجوع حتى لا تبقى الهاندلرات السابقة فعالة
            _reset_state(user_id)
            try:
                bot.edit_message_text("⬅️ رجعناك للقائمة.", chat_id, call.message.message_id)
            except Exception:
                # لو الرسالة الأصلية غير قابلة للتعديل أو انحذفت، نكمل برسالة جديدة
                pass
            bot.send_message(chat_id, "اختار من القائمة:", reply_markup=units_bills_menu_inline())
            return bot.answer_callback_query(call.id)

        bot.answer_callback_query(call.id)

    # ===== صفحات وحدات سيرياتيل/MTN =====
    def _send_syr_units_page(chat_id, page=0, message_id=None):
        # 🔧 زرع مفاتيح كل كمية (idempotent)
        for u in SYRIATEL_UNITS:
            ensure_feature(key_units("Syriatel", u['name']), f"وحدات سيرياتيل — {u['name']}", default_active=True)

        items = [(idx, _unit_label(u)) for idx, u in enumerate(SYRIATEL_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="syrunits", back_data="ubm:back")
        txt = with_cancel_hint(banner("🎯 اختار كمية الوحدات", [f"صفحة {page+1}/{pages}"]))
        if message_id is not None:
            bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, txt, reply_markup=kb)

    def _send_mtn_units_page(chat_id, page=0, message_id=None):
        # 🔧 زرع مفاتيح كل كمية (idempotent)
        for u in MTN_UNITS:
            ensure_feature(key_units("MTN", u['name']), f"وحدات MTN — {u['name']}", default_active=True)

        items = [(idx, _unit_label(u)) for idx, u in enumerate(MTN_UNITS)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS, prefix="mtnunits", back_data="ubm:back")
        txt = with_cancel_hint(banner("🎯 اختار كمية الوحدات", [f"صفحة {page+1}/{pages}"]))
        if message_id is not None:
            bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, txt, reply_markup=kb)
            
    def _send_syr_kazia_page(chat_id, page=0, message_id=None):
        # زرع مفاتيح المبالغ (idempotent)
        for it in KAZIA_OPTIONS_SYR:
            ensure_feature(key_kazia("Syriatel", it["amount"]), f"كازية سيرياتيل — {int(it['amount']):,} ل.س", default_active=True)

        items = [(idx, _kz_label(it)) for idx, it in enumerate(KAZIA_OPTIONS_SYR)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS,
                                                 prefix="syrkz", back_data="ubm:back")
        txt = with_cancel_hint(banner("🎯 اختر المبلغ (جملة كازية سيرياتيل)", [f"صفحة {page+1}/{pages}"]))
        if message_id is not None:
            bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
        else:
            bot.send_message(chat_id, txt, reply_markup=kb)

    def _send_mtn_kazia_page(chat_id, page=0, message_id=None):
        for it in KAZIA_OPTIONS_MTN:
            ensure_feature(key_kazia("MTN", it["amount"]), f"كازية MTN — {int(it['amount']):,} ل.س", default_active=True)

        items = [(idx, _kz_label(it)) for idx, it in enumerate(KAZIA_OPTIONS_MTN)]
        kb, pages = _build_paged_inline_keyboard(items, page=page, page_size=PAGE_SIZE_UNITS,
                                                 prefix="mtnkz", back_data="ubm:back")
        txt = with_cancel_hint(banner("🎯 اختر المبلغ (جملة كازية MTN)", [f"صفحة {page+1}/{pages}"]))
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

            # 🔒 منع الاختيار إن كانت الكمية مقفلة
            if require_feature_or_alert(bot, chat_id, key_units("Syriatel", unit['name']), f"وحدات سيرياتيل — {unit['name']}"):
                return bot.answer_callback_query(call.id)

            user_states[user_id] = {"step": "syr_unit_number", "unit": unit}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text(
                with_cancel_hint("📱 ابعت الرقم/الكود اللي بيبدأ بـ 093 أو 098 أو 099:"),
                chat_id, call.message.message_id, reply_markup=kb
            )
            return bot.answer_callback_query(call.id, text=_unit_label(unit))

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

            # 🔒 منع الاختيار إن كانت الكمية مقفلة
            if require_feature_or_alert(bot, chat_id, key_units("MTN", unit['name']), f"وحدات MTN — {unit['name']}"):
                return bot.answer_callback_query(call.id)

            user_states[user_id] = {"step": "mtn_unit_number", "unit": unit}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text(
                with_cancel_hint("📱 ابعت الرقم/الكود اللي بيبدأ بـ 094 أو 095 أو 096:"),
                chat_id, call.message.message_id, reply_markup=kb
            )
            return bot.answer_callback_query(call.id, text=_unit_label(unit))

        bot.answer_callback_query(call.id)

    # ===== كولباك كازية سيرياتيل =====
    @bot.callback_query_handler(func=lambda call: call.data.startswith("syrkz:"))
    def syr_kazia_inline_handler(call):
        parts = call.data.split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "page":
            page = int(parts[2]) if len(parts) > 2 else 0
            _send_syr_kazia_page(chat_id, page=page, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "sel":
            idx = int(parts[2])
            it = KAZIA_OPTIONS_SYR[idx]
            # فحص ميزة المبلغ المحدد
            if require_feature_or_alert(bot, chat_id, key_kazia("Syriatel", it["amount"]),
                                        f"كازية سيرياتيل — {int(it['amount']):,} ل.س"):
                return bot.answer_callback_query(call.id)

            user_states[user_id] = {"step": "syr_kz_code", "kz": it}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text(
                with_cancel_hint("⌨️ ادخل كود كازية سيرياتيل:"),
                chat_id, call.message.message_id, reply_markup=kb
            )
            return bot.answer_callback_query(call.id, text=_kz_label(it))

        bot.answer_callback_query(call.id)

    # ===== كولباك كازية MTN =====
    @bot.callback_query_handler(func=lambda call: call.data.startswith("mtnkz:"))
    def mtn_kazia_inline_handler(call):
        parts = call.data.split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        if action == "page":
            page = int(parts[2]) if len(parts) > 2 else 0
            _send_mtn_kazia_page(chat_id, page=page, message_id=call.message.message_id)
            return bot.answer_callback_query(call.id)

        if action == "sel":
            idx = int(parts[2])
            it = KAZIA_OPTIONS_MTN[idx]
            if require_feature_or_alert(bot, chat_id, key_kazia("MTN", it["amount"]),
                                        f"كازية MTN — {int(it['amount']):,} ل.س"):
                return bot.answer_callback_query(call.id)

            user_states[user_id] = {"step": "mtn_kz_code", "kz": it}
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            bot.edit_message_text(
                with_cancel_hint("⌨️ ادخل كود كازية MTN:"),
                chat_id, call.message.message_id, reply_markup=kb
            )
            return bot.answer_callback_query(call.id, text=_kz_label(it))

        bot.answer_callback_query(call.id)

    # ===== سيرياتيل كازية: إدخال الكود ثم تأكيد =====
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_kz_code")
    def syr_kz_code(msg):
        user_id = msg.from_user.id
        code = msg.text.strip()
        state = user_states[user_id]
        state["code"] = code
        state["step"] = "syr_kz_confirm"
        it = state["kz"]
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد الطلب", "syr_kz_final_confirm"))
        lines = [
            f"المبلغ: {_fmt_syp(int(it['amount']))}",
            f"السعر:  {_fmt_syp(int(it['price']))}",
            f"الكود:   {code}",
            "نكمّل الطلب؟ 😉"
        ]
        bot.send_message(msg.chat.id, with_cancel_hint(banner("🧾 تأكيد عملية (جملة كازية سيرياتيل)", lines)), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "syr_kz_final_confirm")
    def syr_kz_final_confirm(call):
        user_id = call.from_user.id
        if confirm_guard(bot, call, "syr_kz_final_confirm"):
            return
        name = _user_name(call)

        if is_maintenance():
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "syr_kazia", "جملة (كازية) سيرياتيل"):
            return

        state = user_states.get(user_id, {})
        it = state.get("kz") or {}
        code = state.get("code") or ""
        amount = int(it.get("amount") or 0)
        price  = int(it.get("price")  or 0)

        # فحص الميزة للمبلغ المختار
        if require_feature_or_alert(bot, call.message.chat.id, key_kazia("Syriatel", amount),
                                    f"كازية سيرياتيل — {amount:,} ل.س"):
            return

        available = get_available_balance(user_id)
        if available < price:
            missing = price - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}",
                                                        f"المطلوب: {_fmt_syp(price)}",
                                                        f"الناقص: {_fmt_syp(missing)}"])),
                reply_markup=kb
            )

        # إنشاء HOLD
        hold_id = None
        try:
            resp = create_hold(user_id, price, f"حجز جملة كازية سيرياتيل - {amount:,} ل.س")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.\n\n{CANCEL_HINT}")

        bal_now = get_balance(user_id)
        admin_msg = (
            f"🧾 طلب جملة (كازية) سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🔖 المبلغ: {amount:,} ل.س\n"
            f"💵 السعر: {price:,} ل.س\n"
            f"🔐 الكود: <code>{code}</code>\n"
            f"💼 رصيد المستخدم الآن: {bal_now:,} ل.س\n"
            f"(type=syr_kazia)"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "syr_kazia",
                "code": code,
                "amount": amount,
                "price": price,
                "reserved": price,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["هننجّزها بسرعة ✌️ وهيوصلك إشعار أول ما نكمّل."])
        )
        user_states[user_id]["step"] = "wait_admin_syr_kazia"
    # ===== MTN كازية: كود ثم رقم الكازية ثم تأكيد =====
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_kz_code")
    def mtn_kz_code(msg):
        user_id = msg.from_user.id
        code = msg.text.strip()
        state = user_states[user_id]
        state["code"] = code
        state["step"] = "mtn_kz_number"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, with_cancel_hint("📍 أدخل رقم كازية MTN:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_kz_number")
    def mtn_kz_number(msg):
        user_id = msg.from_user.id
        station = msg.text.strip()
        state = user_states[user_id]
        state["station"] = station
        state["step"] = "mtn_kz_confirm"
        it = state["kz"]
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد الطلب", "mtn_kz_final_confirm"))
        lines = [
            f"المبلغ: {_fmt_syp(int(it['amount']))}",
            f"السعر:  {_fmt_syp(int(it['price']))}",
            f"الكود:   {state['code']}",
            f"رقم الكازية: {station}",
            "نكمّل الطلب؟ 😉"
        ]
        bot.send_message(msg.chat.id, with_cancel_hint(banner("🧾 تأكيد عملية (جملة كازية MTN)", lines)), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "mtn_kz_final_confirm")
    def mtn_kz_final_confirm(call):
        user_id = call.from_user.id
        if confirm_guard(bot, call, "mtn_kz_final_confirm"):
            return
        name = _user_name(call)

        if is_maintenance():
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "mtn_kazia", "جملة (كازية) MTN"):
            return

        state = user_states.get(user_id, {})
        it = state.get("kz") or {}
        code = state.get("code") or ""
        station = state.get("station") or ""
        amount = int(it.get("amount") or 0)
        price  = int(it.get("price")  or 0)

        if require_feature_or_alert(bot, call.message.chat.id, key_kazia("MTN", amount),
                                    f"كازية MTN — {amount:,} ل.س"):
            return

        available = get_available_balance(user_id)
        if available < price:
            missing = price - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}",
                                                            f"المطلوب: {_fmt_syp(price)}",
                                                            f"الناقص: {_fmt_syp(missing)}"])),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, price, f"حجز جملة كازية MTN - {amount:,} ل.س")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.\n\n{CANCEL_HINT}")

        bal_now = get_balance(user_id)
        admin_msg = (
            f"🧾 طلب جملة (كازية) MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🔖 المبلغ: {amount:,} ل.س\n"
            f"💵 السعر: {price:,} ل.س\n"
            f"🔐 الكود: <code>{code}</code>\n"
            f"🏷️ رقم الكازية: <code>{station}</code>\n"
            f"💼 رصيد المستخدم الآن: {bal_now:,} ل.س\n"
            f"(type=mtn_kazia)"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "mtn_kazia",
                "code": code,
                "station_number": station,
                "amount": amount,
                "price": price,
                "reserved": price,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["هننجّزها بسرعة ✌️ وهيوصلك إشعار أول ما نكمّل."])
        )
        user_states[user_id]["step"] = "wait_admin_mtn_kazia"

    # ===== إلغاء عام (زر cancel_all) =====
    @bot.callback_query_handler(func=lambda call: call.data == "cancel_all")
    def cancel_all_handler(call):
        user_id = call.from_user.id
        _reset_state(user_id)
        nm = _user_name(call)
        try:
            remove_inline_keyboard(bot, call.message)
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            banner("❌ تم الإلغاء", [f"يا {nm}، رجعناك للقائمة. 😉", CANCEL_HINT]),
            reply_markup=units_bills_menu_inline()
        )
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
            
    # ===== رجوع (ReplyKeyboard) — يعمل في جميع مراحل الريلاي كيبورد =====
    @bot.message_handler(func=lambda msg: msg.text == "⬅️ رجوع")
    def reply_back_btn(msg):
        user_id = msg.from_user.id
        # ✅ تصفير الحالة وإزالة أي كيبورد قديم
        _reset_state(user_id)
        try:
            bot.send_message(msg.chat.id, "⬅️ رجعناك للقائمة.", reply_markup=types.ReplyKeyboardRemove())
        except Exception:
            pass
        # ✅ إظهار القائمة الرئيسية (Inline)
        bot.send_message(msg.chat.id, "اختار من القائمة:", reply_markup=units_bills_menu_inline())


    # ===================================================================
    #   (التوافق) مسارات الـ ReplyKeyboard القديمة — من غير حذف
    # ===================================================================

    ########## وحدات سيرياتيل (Reply) ##########
    @bot.message_handler(func=lambda msg: msg.text == "🔴 وحدات سيرياتيل")
    def syr_units_menu(msg):
        # صيانة أو إيقاف ميزة؟
        if is_maintenance():
            return bot.send_message(msg.chat.id, maintenance_message())
        if block_if_disabled(bot, msg.chat.id, "syr_unit", "وحدات سيرياتيل"):
            return
        user_id = msg.from_user.id

        # 🔧 زرع مفاتيح الكميات
        for u in SYRIATEL_UNITS:
            ensure_feature(key_units("Syriatel", u['name']), f"وحدات سيرياتيل — {u['name']}")

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for u in SYRIATEL_UNITS:
            kb.add(types.KeyboardButton(_unit_label(u)))
        kb.add(types.KeyboardButton("⬅️ رجوع"))
        user_states[user_id] = {"step": "select_syr_unit"}
        bot.send_message(msg.chat.id, with_cancel_hint("🎯 اختار كمية الوحدات:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "select_syr_unit")
    def syr_unit_select(msg):
        user_id = msg.from_user.id
        unit = next((u for u in SYRIATEL_UNITS if _unit_label(u) == msg.text), None)
        if not unit:
            return bot.send_message(msg.chat.id, "⚠️ اختار كمية من القائمة لو سمحت.\n\n" + CANCEL_HINT)

        # 🔒 منع التقدّم إن كانت الكمية مقفلة
        if require_feature_or_alert(bot, msg.chat.id, key_units("Syriatel", unit['name']), f"وحدات سيرياتيل — {unit['name']}"):
            return

        user_states[user_id] = {"step": "syr_unit_number", "unit": unit}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, with_cancel_hint("📱 ابعت الرقم/الكود اللي بيبدأ بـ 093 أو 098 أو 099:"), reply_markup=kb)

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
            with_cancel_hint(banner("🧾 تأكيد العملية", [f"هنبعت {unit['name']} بسعر {_fmt_syp(unit['price'])} للرقم:", number])),
            reply_markup=kb
        )

    # تأكيد نهائي (HOLD) — سيرياتيل وحدات
    @bot.callback_query_handler(func=lambda call: call.data == "syr_unit_final_confirm")
    def syr_unit_final_confirm(call):
        user_id = call.from_user.id
        # ✅ حارس موحّد: يشيل الكيبورد فقط + Debounce
        if confirm_guard(bot, call, "syr_unit_final_confirm"):
            return
        name = _user_name(call)

        # صيانة أو إيقاف ميزة؟
        if is_maintenance():
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "syr_unit", "وحدات سيرياتيل"):
            return

        state = user_states.get(user_id, {})
        unit = state.get("unit") or {}
        number = state.get("number")
        unit_name = unit.get("name") or "وحدات سيرياتيل"

        # السعر قبل الخصم
        price_before = int(unit.get("price") or 0)

        # ✅ تطبيق خصم للوحدات (سيرياتيل) — الكازية مستثناة بالكامل
        try:
            revalidate_user_discount(bot, user_id)
        except Exception:
            pass
        price, applied_disc = apply_discount(user_id, price_before)

        # 🔒 فحص الكمية نفسها قبل التنفيذ
        if require_feature_or_alert(bot, call.message.chat.id, key_units("Syriatel", unit_name), f"وحدات سيرياتيل — {unit_name}"):
            return

        available = get_available_balance(user_id)
        if available < price:
            missing = price - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(price)}", f"الناقص: {_fmt_syp(missing)}"])),
                reply_markup=kb
            )

        # إنشاء الحجز على السعر بعد الخصم
        hold_id = None
        try:
            resp = create_hold(user_id, price, f"حجز وحدات سيرياتيل - {unit_name}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.\n\n{CANCEL_HINT}")

        # رسالة للإدمن
        bal_now = get_balance(user_id)
        if applied_disc:
            price_block = (
                f"💰 السعر قبل الخصم: {price_before:,} ل.س\n"
                f"٪ الخصم: {int(applied_disc.get('percent') or 0)}٪\n"
                f"💵 السعر بعد الخصم: {price:,} ل.س\n"
            )
        else:
            price_block = f"💵 السعر: {price:,} ل.س\n"

        admin_msg = (
            f"🧾 طلب وحدات سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم/الكود: <code>{number}</code>\n"
            f"🔖 الكمية: {unit_name}\n"
            f"{price_block}"
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
                "price": int(price),                     # بعد الخصم
                "price_before": int(price_before),       # قبل الخصم
                "discount": (
                    {"id": (applied_disc or {}).get("id"),
                     "percent": (applied_disc or {}).get("percent"),
                     "before": int(price_before),
                     "after": int(price)}
                    if applied_disc else None
                ),
                "reserved": int(price),
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        disc = (user_states.get(user_id, {}) or {}).get('discount')
        msg_lines = [
            "هننجّزها بسرعة ✌️ وهيوصلك إشعار أول ما نكمّل.",
        ]
        if disc:
            msg_lines = [
                f"💵 السعر قبل الخصم: {int(disc.get('before') or 0):,} ل.س",
                f"٪ الخصم: {int(disc.get('percent') or 0)}٪",
                f"💵 السعر بعد الخصم: {int(disc.get('after') or 0):,} ل.س",
            ] + msg_lines

        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", msg_lines)
        )

        user_states[user_id]["step"] = "wait_admin_syr_unit"

    ########## وحدات MTN (Reply) ##########
    @bot.message_handler(func=lambda msg: msg.text == "🟡 وحدات MTN")
    def mtn_units_menu(msg):
        if is_maintenance():
            return bot.send_message(msg.chat.id, maintenance_message())
        if block_if_disabled(bot, msg.chat.id, "mtn_unit", "وحدات MTN"):
            return
        user_id = msg.from_user.id

        # 🔧 زرع مفاتيح الكميات
        for u in MTN_UNITS:
            ensure_feature(key_units("MTN", u['name']), f"وحدات MTN — {u['name']}")

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for u in MTN_UNITS:
            kb.add(types.KeyboardButton(_unit_label(u)))
        kb.add(types.KeyboardButton("⬅️ رجوع"))
        user_states[user_id] = {"step": "select_mtn_unit"}
        bot.send_message(msg.chat.id, with_cancel_hint("🎯 اختار كمية الوحدات:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "select_mtn_unit")
    def mtn_unit_select(msg):
        user_id = msg.from_user.id
        unit = next((u for u in MTN_UNITS if _unit_label(u) == msg.text), None)
        if not unit:
            return bot.send_message(msg.chat.id, "⚠️ اختار كمية من القائمة لو سمحت.\n\n" + CANCEL_HINT)

        # 🔒 منع التقدّم إن كانت الكمية مقفلة
        if require_feature_or_alert(bot, msg.chat.id, key_units("MTN", unit['name']), f"وحدات MTN — {unit['name']}"):
            return

        user_states[user_id] = {"step": "mtn_unit_number", "unit": unit}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, with_cancel_hint("📱 ابعت الرقم/الكود اللي بيبدأ بـ 094 أو 095 أو 096:"), reply_markup=kb)

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
            with_cancel_hint(banner("🧾 تأكيد العملية", [f"هنبعت {unit['name']} بسعر {_fmt_syp(unit['price'])} للرقم:", number])),
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "mtn_unit_final_confirm")
    def mtn_unit_final_confirm(call):
        user_id = call.from_user.id
        if confirm_guard(bot, call, "mtn_unit_final_confirm"):
            return
        name = _user_name(call)

        if is_maintenance():
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "mtn_unit", "وحدات MTN"):
            return

        state = user_states.get(user_id, {})
        unit = state.get("unit") or {}
        number = state.get("number")
        unit_name = unit.get("name") or "وحدات MTN"

        # السعر قبل الخصم
        price_before = int(unit.get("price") or 0)

        # ✅ تطبيق خصم للوحدات (MTN) — الكازية مستثناة بالكامل
        try:
            revalidate_user_discount(bot, user_id)
        except Exception:
            pass
        price, applied_disc = apply_discount(user_id, price_before)

        # 🔒 فحص الكمية نفسها قبل التنفيذ
        if require_feature_or_alert(bot, call.message.chat.id, key_units("MTN", unit_name), f"وحدات MTN — {unit_name}"):
            return

        available = get_available_balance(user_id)
        if available < price:
            missing = price - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(price)}", f"الناقص: {_fmt_syp(missing)}"])),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, price, f"حجز وحدات MTN - {unit_name}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.\n\n{CANCEL_HINT}")

        bal_now = get_balance(user_id)
        if applied_disc:
            price_block = (
                f"💰 السعر قبل الخصم: {price_before:,} ل.س\n"
                f"٪ الخصم: {int(applied_disc.get('percent') or 0)}٪\n"
                f"💵 السعر بعد الخصم: {price:,} ل.س\n"
            )
        else:
            price_block = f"💵 السعر: {price:,} ل.س\n"

        admin_msg = (
            f"🧾 طلب وحدات MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم/الكود: <code>{number}</code>\n"
            f"🔖 الكمية: {unit_name}\n"
            f"{price_block}"
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
                "price": int(price),                     # بعد الخصم
                "price_before": int(price_before),       # قبل الخصم
                "discount": (
                    {"id": (applied_disc or {}).get("id"),
                     "percent": (applied_disc or {}).get("percent"),
                     "before": int(price_before),
                     "after": int(price)}
                    if applied_disc else None
                ),
                "reserved": int(price),
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["هننجّزها بسرعة ✌️ وهيوصلك إشعار أول ما نكمّل."])
        )
        user_states[user_id]["step"] = "wait_admin_mtn_unit"

    ########## فاتورة سيرياتيل ##########
    @bot.message_handler(func=lambda msg: msg.text == "🔴 فاتورة سيرياتيل")
    def syr_bill_entry(msg):
        if is_maintenance():
            return bot.send_message(msg.chat.id, maintenance_message())
        if block_if_disabled(bot, msg.chat.id, "syr_bill", "فاتورة سيرياتيل"):
            return
        user_id = msg.from_user.id
        user_states[user_id] = {"step": "syr_bill_number"}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, with_cancel_hint("📱 ابعت رقم سيرياتيل اللي هتدفع فاتورته:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_bill_number")
    def syr_bill_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        user_states[user_id]["number"] = number
        user_states[user_id]["step"] = "syr_bill_number_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_syr_bill_number"), ("✔️ تأكيد", "confirm_syr_bill_number"))
        bot.send_message(msg.chat.id, with_cancel_hint(banner("🧷 تأكيد الرقم", [number])), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_number")
    def edit_syr_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_number"
        bot.send_message(call.message.chat.id, with_cancel_hint("📱 ابعت رقم الموبايل تاني:"))

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_number")
    def confirm_syr_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(call.message.chat.id, with_cancel_hint("💵 ابعت مبلغ الفاتورة بالليرة:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "syr_bill_amount")
    def syr_bill_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except Exception:
            return bot.send_message(msg.chat.id, "⚠️ ادخل مبلغ صحيح بالأرقام.\n\n" + CANCEL_HINT)
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "syr_bill_amount_confirm"

        # ✅ تطبيق خصم على مبلغ الفاتورة (قبل حساب الأجور) — الكازية خارج المعادلة
        amount_before = int(amount)
        try:
            revalidate_user_discount(bot, user_id)
        except Exception:
            pass
        amount_after, applied_disc = apply_discount(user_id, amount_before)

        fee = amount_after * 7 // 100  # أجور بعد الخصم (أنصف للعميل)
        amount_with_fee = amount_after + fee

        user_states[user_id]["amount_after"] = amount_after
        user_states[user_id]["fee"] = fee
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["discount"] = (
            {"before": int(amount_before), "after": int(amount_after),
             "percent": (applied_disc or {}).get("percent"), "id": (applied_disc or {}).get("id")}
            if applied_disc else None
        )

        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_syr_bill_amount"), ("✔️ تأكيد", "confirm_syr_bill_amount"))
        if applied_disc:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ قبل الخصم: {_fmt_syp(amount_before)}",
                f"٪ الخصم: {int((applied_disc or {}).get('percent') or 0)}٪",
                f"المبلغ بعد الخصم: {_fmt_syp(amount_after)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        else:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ: {_fmt_syp(amount_before)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        bot.send_message(msg.chat.id, with_cancel_hint(banner("تفاصيل الفاتورة (سيرياتيل)", lines)), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_syr_bill_amount")
    def edit_syr_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "syr_bill_amount"
        bot.send_message(call.message.chat.id, with_cancel_hint("💵 ابعت مبلغ الفاتورة تاني:"))

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_syr_bill_amount")
    def confirm_syr_bill_amount(call):
        user_id = call.from_user.id
        amount_after = int(user_states[user_id].get("amount_after") or user_states[user_id]["amount"])
        fee = int(user_states[user_id].get("fee") or (amount_after * 7 // 100))
        amount_with_fee = int(user_states[user_id].get("amount_with_fee") or (amount_after + fee))
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "syr_bill_final_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد", "final_confirm_syr_bill"))
        disc = user_states[user_id].get("discount")
        if disc:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ قبل الخصم: {_fmt_syp(int(disc['before']))}",
                f"٪ الخصم: {int(disc.get('percent') or 0)}٪",
                f"المبلغ بعد الخصم: {_fmt_syp(amount_after)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        else:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ: {_fmt_syp(amount_after)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        bot.send_message(call.message.chat.id, with_cancel_hint(banner("تفاصيل الفاتورة (سيرياتيل)", lines)), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_syr_bill")
    def final_confirm_syr_bill(call):
        user_id = call.from_user.id
        if confirm_guard(bot, call, "final_confirm_syr_bill"):
            return
        name = _user_name(call)

        if is_maintenance():
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "syr_bill", "فاتورة سيرياتيل"):
            return

        state = user_states.get(user_id, {})
        number = state.get("number")
        amount_before = int(state.get("amount") or 0)
        amount_after = int(state.get("amount_after") or amount_before)
        fee = int(state.get("fee") or (amount_after * 7 // 100))
        total  = int(state.get("amount_with_fee") or (amount_after + fee))

        available = get_available_balance(user_id)
        if available < total:
            missing = total - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(total)}", f"الناقص: {_fmt_syp(missing)}"])),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, total, f"حجز فاتورة سيرياتيل للرقم {number}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.\n\n{CANCEL_HINT}")

        bal_now = get_balance(user_id)

        disc = state.get("discount")
        if disc:
            price_block = (
                f"💵 المبلغ قبل الخصم: {int(amount_before):,} ل.س\n"
                f"٪ الخصم: {int(disc.get('percent') or 0)}٪\n"
                f"💵 المبلغ بعد الخصم: {int(amount_after):,} ل.س\n"
            )
        else:
            price_block = f"💵 المبلغ: {int(amount_after):,} ل.س\n"

        admin_msg = (
            f"🧾 دفع فاتورة سيرياتيل\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم: <code>{number}</code>\n"
            f"{price_block}"
            f"🧾 الإجمالي مع العمولة: {int(total):,} ل.س\n"
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
                "amount": int(amount_before),          # المبلغ الأصلي المستحق للجهة
                "price": int(amount_after),            # ما سيدفعه العميل بعد الخصم
                "price_before": int(amount_before),    # لتسجيل استخدام الخصم

                "fee": int(fee),
                "total": int(total),
                "reserved": int(total),
                "discount": disc,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["هننجّزها بسرعة ✌️ وهيوصلك إشعار أول ما نكمّل."])
        )
        user_states[user_id]["step"] = "wait_admin_syr_bill"

    ########## فاتورة MTN ##########
    @bot.message_handler(func=lambda msg: msg.text == "🟡 فاتورة MTN")
    def mtn_bill_entry(msg):
        if is_maintenance():
            return bot.send_message(msg.chat.id, maintenance_message())
        if block_if_disabled(bot, msg.chat.id, "mtn_bill", "فاتورة MTN"):
            return
        user_id = msg.from_user.id
        user_states[user_id] = {"step": "mtn_bill_number"}
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(msg.chat.id, with_cancel_hint("📱 ابعت رقم MTN اللي هتدفع فاتورته:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_bill_number")
    def mtn_bill_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        user_states[user_id]["number"] = number
        user_states[user_id]["step"] = "mtn_bill_number_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_mtn_bill_number"), ("✔️ تأكيد", "confirm_mtn_bill_number"))
        bot.send_message(msg.chat.id, with_cancel_hint(banner("🧷 تأكيد الرقم", [number])), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_number")
    def edit_mtn_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_number"
        bot.send_message(call.message.chat.id, with_cancel_hint("📱 ابعت رقم الموبايل تاني:"))

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_number")
    def confirm_mtn_bill_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
        bot.send_message(call.message.chat.id, with_cancel_hint("💵 ابعت مبلغ الفاتورة بالليرة:"), reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "mtn_bill_amount")
    def mtn_bill_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except Exception:
            return bot.send_message(msg.chat.id, "⚠️ ادخل مبلغ صحيح بالأرقام.\n\n" + CANCEL_HINT)
        user_states[user_id]["amount"] = amount
        user_states[user_id]["step"] = "mtn_bill_amount_confirm"

        # ✅ تطبيق خصم على مبلغ الفاتورة (قبل أجور الخدمة)
        amount_before = int(amount)
        try:
            revalidate_user_discount(bot, user_id)
        except Exception:
            pass
        amount_after, applied_disc = apply_discount(user_id, amount_before)

        fee = amount_after * 7 // 100  # أجور بعد الخصم
        amount_with_fee = amount_after + fee

        user_states[user_id]["amount_after"] = amount_after
        user_states[user_id]["fee"] = fee
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["discount"] = (
            {"before": int(amount_before), "after": int(amount_after),
             "percent": (applied_disc or {}).get("percent"), "id": (applied_disc or {}).get("id")}
            if applied_disc else None
        )

        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✏️ تعديل", "edit_mtn_bill_amount"), ("✔️ تأكيد", "confirm_mtn_bill_amount"))
        if applied_disc:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ قبل الخصم: {_fmt_syp(amount_before)}",
                f"٪ الخصم: {int((applied_disc or {}).get('percent') or 0)}٪",
                f"المبلغ بعد الخصم: {_fmt_syp(amount_after)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        else:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ: {_fmt_syp(amount_before)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        bot.send_message(msg.chat.id, with_cancel_hint(banner("تفاصيل الفاتورة (MTN)", lines)), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_mtn_bill_amount")
    def edit_mtn_bill_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "mtn_bill_amount"
        bot.send_message(call.message.chat.id, with_cancel_hint("💵 ابعت مبلغ الفاتورة تاني:"))

    @bot.callback_query_handler(func=lambda call: call.data == "confirm_mtn_bill_amount")
    def confirm_mtn_bill_amount(call):
        user_id = call.from_user.id
        amount_after = int(user_states[user_id].get("amount_after") or user_states[user_id]["amount"])
        fee = int(user_states[user_id].get("fee") or (amount_after * 7 // 100))
        amount_with_fee = int(user_states[user_id].get("amount_with_fee") or (amount_after + fee))
        user_states[user_id]["amount_with_fee"] = amount_with_fee
        user_states[user_id]["step"] = "mtn_bill_final_confirm"
        kb = make_inline_buttons(("❌ إلغاء", "cancel_all"), ("✔️ تأكيد", "final_confirm_mtn_bill"))
        disc = user_states[user_id].get("discount")
        if disc:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ قبل الخصم: {_fmt_syp(int(disc['before']))}",
                f"٪ الخصم: {int(disc.get('percent') or 0)}٪",
                f"المبلغ بعد الخصم: {_fmt_syp(amount_after)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        else:
            lines = [
                f"الرقم: {user_states[user_id]['number']}",
                f"المبلغ: {_fmt_syp(amount_after)}",
                f"أجور الخدمة: {_fmt_syp(fee)}",
                f"الإجمالي: {_fmt_syp(amount_with_fee)}",
                "نكمّل؟ 😉"
            ]
        bot.send_message(call.message.chat.id, with_cancel_hint(banner("تفاصيل الفاتورة (MTN)", lines)), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "final_confirm_mtn_bill")
    def final_confirm_mtn_bill(call):
        user_id = call.from_user.id
        if confirm_guard(bot, call, "final_confirm_mtn_bill"):
            return
        name = _user_name(call)

        if is_maintenance():
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "mtn_bill", "فاتورة MTN"):
            return

        state = user_states.get(user_id, {})
        number = state.get("number")
        amount_before = int(state.get("amount") or 0)
        amount_after = int(state.get("amount_after") or amount_before)
        fee = int(state.get("fee") or (amount_after * 7 // 100))
        total  = int(state.get("amount_with_fee") or (amount_after + fee))

        available = get_available_balance(user_id)
        if available < total:
            missing = total - (available or 0)
            kb = make_inline_buttons(("❌ إلغاء", "cancel_all"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(banner("❌ رصيدك مش مكفّي", [f"متاحك: {_fmt_syp(available)}", f"المطلوب: {_fmt_syp(total)}", f"الناقص: {_fmt_syp(missing)}"])),
                reply_markup=kb
            )

        hold_id = None
        try:
            resp = create_hold(user_id, total, f"حجز فاتورة MTN للرقم {number}")
            hold_id = (None if getattr(resp, "error", None) else getattr(resp, "data", None))
        except Exception as e:
            logging.exception("create_hold failed: %s", e)

        if not hold_id:
            return bot.send_message(call.message.chat.id, f"⚠️ يا {name}، حصل عطل بسيط في إنشاء الحجز. جرّب تاني بعد دقيقة.\n\n{CANCEL_HINT}")

        bal_now = get_balance(user_id)

        disc = state.get("discount")
        if disc:
            price_block = (
                f"💵 المبلغ قبل الخصم: {int(amount_before):,} ل.س\n"
                f"٪ الخصم: {int(disc.get('percent') or 0)}٪\n"
                f"💵 المبلغ بعد الخصم: {int(amount_after):,} ل.س\n"
            )
        else:
            price_block = f"💵 المبلغ: {int(amount_after):,} ل.س\n"

        admin_msg = (
            f"🧾 دفع فاتورة MTN\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"📱 الرقم: <code>{number}</code>\n"
            f"{price_block}"
            f"🧾 الإجمالي مع العمولة: {int(total):,} ل.س\n"
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
                "amount": int(amount_before),          # المبلغ الأصلي المستحق للجهة
                "price": int(amount_after),
                "price_before": int(amount_before),    # لتسجيل استخدام الخصم
                "fee": int(fee),
                "total": int(total),
                "reserved": int(total),
                "discount": disc,
                "hold_id": hold_id,
            }
        )
        process_queue(bot)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك في السكة 🚀", ["هننجّزها بسرعة ✌️ وهيوصلك إشعار أول ما نكمّل."])
        )
        user_states[user_id]["step"] = "wait_admin_mtn_bill"

# واجهة يستدعيها main.py
def register(bot):
    register_bill_and_units(bot, {})
