# -*- coding: utf-8 -*-
# handlers/companies_transfer.py — تحويل من المحفظة لاستلامه كاش من شركات الحوالات
# • /cancel للإلغاء في أي وقت
# • confirm_guard عند التأكيد (يحذف الكيبورد فقط + Debounce)
# • رسائل محسّنة وإيموجي وبانر

# استيرادات مرِنة موجودة
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

# حارس التأكيد الموحّد
try:
    from services.ui_guards import confirm_guard
except Exception:
    from ui_guards import confirm_guard

from telebot import types
from services.wallet_service import (
    add_purchase,
    get_balance,
    has_sufficient_balance,
    deduct_balance,           # احتياطي لمسارات قديمة
    register_user_if_not_exist,
    create_hold,              # ✅ حجز
    capture_hold,             # ✅ تصفية الحجز
    release_hold,             # ✅ فكّ الحجز
    get_available_balance,    # ✅ المتاح = balance - held
)
from database.db import get_table
from config import ADMIN_MAIN_ID
from handlers import keyboards
from services.queue_service import add_pending_request, process_queue
import logging

# صيانة + أعلام المزايا (Feature Flags اختيارية)
try:
    from services.system_service import is_maintenance, maintenance_message
except Exception:
    def is_maintenance(): return False
    def maintenance_message(): return "🔧 النظام تحت الصيانة مؤقتًا. جرّب لاحقًا."

try:
    from services.feature_flags import block_if_disabled  # يتطلب مفتاح "companies_transfer"
except Exception:
    def block_if_disabled(bot, chat_id, flag_key, nice_name):
        return False

from services.state_adapter import UserStateDictLike
user_states = UserStateDictLike()
COMMISSION_PER_50000 = 1500

# ===== مظهر الرسائل + /cancel =====
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel أو الغاء/كانسل للإلغاء في أي وقت."

def banner(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def with_cancel_hint(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _user_name(bot, user_id: int) -> str:
    """اسم مختصر لطيف للرسائل."""
    try:
        ch = bot.get_chat(user_id)
        name = (getattr(ch, "first_name", None) or getattr(ch, "full_name", "") or "").strip()
        return name or "صاحبنا"
    except Exception:
        return "صاحبنا"

def _service_unavailable_guard(bot, chat_id) -> bool:
    """يرجع True إذا الخدمة غير متاحة (صيانة/مقفلة عبر Feature Flag)."""
    if is_maintenance():
        bot.send_message(chat_id, maintenance_message())
        return True
    if block_if_disabled(bot, chat_id, "companies_transfer", "حوالة عبر الشركات"):
        return True
    return False

def calculate_commission(amount: int) -> int:
    # حساب عددي صحيح: عمولة 1500 لكل 50,000 + جزء نسبي
    blocks = amount // 50000
    remainder = amount % 50000
    commission = blocks * COMMISSION_PER_50000
    commission += (remainder * COMMISSION_PER_50000) // 50000
    return int(commission)

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb
    
def _safe_delete(bot, chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        try:
            # نسخة احتياط: إزالة الكيبورد إذا ما أمكن الحذف
            bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
        except Exception:
            pass

def _replace_screen(bot, call, text, reply_markup=None, parse_mode=None):
    """يحذف رسالة الزر الحاليّة ويبعث رسالة جديدة (شاشة واحدة)."""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    _safe_delete(bot, call.message.chat.id, call.message.message_id)
    return bot.send_message(call.message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)

def companies_transfer_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🏦 شركة الهرم", callback_data="company_alharam"),
        types.InlineKeyboardButton("🏦 شركة الفؤاد", callback_data="company_alfouad"),
        types.InlineKeyboardButton("🏦 شركة شخاشير", callback_data="company_shakhashir"),
        types.InlineKeyboardButton("⬅️ رجوع", callback_data="back"),
        types.InlineKeyboardButton("🔄 ابدأ من جديد", callback_data="restart")
    )
    return kb

# تم حذف دالة خاطئة كانت سبب SyntaxError سابقًا (لا حاجة لها الآن)

def register_companies_transfer(bot, history):

    # ===== /cancel العام =====
    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(msg):
        uid = msg.from_user.id
        user_states.pop(uid, None)
        bot.send_message(
            msg.chat.id,
            banner("❌ تم الإلغاء", [f"يا {_user_name(bot, uid)}، رجعناك للقائمة. اختار الشركة 👇"]),
            reply_markup=companies_transfer_menu()
        )
        
    @bot.message_handler(func=lambda m: (m.text or "").strip() in ["الغاء", "إلغاء", "كانسل", "cancel"])
    def cancel_words(m):
        return cancel_cmd(m)

    @bot.message_handler(func=lambda msg: msg.text == "حوالة مالية عبر شركات")
    def open_companies_menu(msg):
        user_id = msg.from_user.id
        if _service_unavailable_guard(bot, msg.chat.id):
            return
        name = _user_name(bot, user_id)
        register_user_if_not_exist(user_id)
        user_states[user_id] = {"step": None}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("companies_menu")

        logging.info(f"[COMPANY][{user_id}] فتح قائمة تحويل الشركات")
        bot.send_message(
            msg.chat.id,
            with_cancel_hint(banner("💸 اختار الشركة اللي تناسبك", ["جاهزين نخلّص لك بسرعة وبأحسن سعر 😉"])),
            reply_markup=companies_transfer_menu()
        )

    # ===== أزرار عامة: رجوع / ابدأ من جديد =====
    @bot.callback_query_handler(func=lambda call: call.data in ["back", "restart"])
    def back_or_restart(call):
        user_id = call.from_user.id
        if _service_unavailable_guard(bot, call.message.chat.id):
            try: bot.answer_callback_query(call.id)
            except Exception: pass
            return

        user_states.pop(user_id, None)
        _replace_screen(
            bot, call,
            "⬅️ رجعناك لقائمة الشركات. اختار من جديد:",
            reply_markup=companies_transfer_menu()
        )

    @bot.callback_query_handler(func=lambda call: call.data in [
        "company_alharam", "company_alfouad", "company_shakhashir"
    ])
    def select_company(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        name = _user_name(bot, user_id)

        company_map = {
            "company_alharam": "شركة الهرم",
            "company_alfouad": "شركة الفؤاد",
            "company_shakhashir": "شركة شخاشير"
        }
        company = company_map[call.data]
        user_states[user_id] = {"step": "show_commission", "company": company}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("companies_menu")
        logging.info(f"[COMPANY][{user_id}] اختار شركة: {company}")

        text = with_cancel_hint(
            f"⚠️ تنويه سريع يا {name}:\n"
            f"• عمولة كل 50,000 ل.س = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام، ابعت بيانات المستفيد ونكمل على طول."
        )
        kb = make_inline_buttons(
            ("✅ ماشي", "company_commission_confirm"),
            ("❌ إلغاء", "company_commission_cancel")
        )
        _replace_screen(bot, call, text, reply_markup=kb)
        return

    @bot.callback_query_handler(func=lambda call: call.data == "company_commission_cancel")
    def company_commission_cancel(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states.pop(user_id, None)
        _replace_screen(
            bot, call,
            banner("✅ تم الإلغاء", [f"يا {name}، لو حابب تقدر تبدأ من جديد في أي وقت.", CANCEL_HINT]),
            reply_markup=companies_transfer_menu()
        )

    @bot.callback_query_handler(func=lambda call: call.data == "company_commission_confirm")
    def company_commission_confirm(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            try: bot.answer_callback_query(call.id)
            except Exception: pass
            return
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_name"
        kb = make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        _replace_screen(
            bot, call,
            with_cancel_hint(f"👤 يا {name}، ابعت اسم المستفيد بالكامل: (الاسم الكنية ابن الأب)"),
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_beneficiary_name")
    def get_beneficiary_name(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        full_name = (msg.text or "").strip()
        if len(full_name) < 5:
            return bot.send_message(msg.chat.id, with_cancel_hint(f"⚠️ يا {name}، اكتب الاسم الثلاثي/الرباعي كاملًا."))
        user_states[user_id]["beneficiary_name"] = full_name
        user_states[user_id]["step"] = "confirm_beneficiary_name"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_beneficiary_name"),
            ("✔️ تأكيد", "beneficiary_name_confirm")
        )
        logging.info(f"[COMPANY][{user_id}] اسم المستفيد: {full_name}")
        bot.send_message(
            msg.chat.id,
            with_cancel_hint(f"👤 تمام يا {name}، الاسم المدخّل:\n{full_name}\n\nنكمل؟"),
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_beneficiary_name")
    def edit_beneficiary_name(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_name"
        _replace_screen(
            bot, call,
            with_cancel_hint(f"👤 تمام يا {name}، ابعت الاسم تاني (الاسم الكنية ابن الأب):"),
            reply_markup=make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        )

    @bot.callback_query_handler(func=lambda call: call.data == "beneficiary_name_confirm")
    def beneficiary_name_confirm(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return

        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_number"
        _replace_screen(
            bot, call,
            with_cancel_hint(f"📱 يا {name}، ابعت رقم المستفيد (لازم يبدأ بـ 09) — 10 أرقام:"),
            reply_markup=make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        )
        
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_beneficiary_number")
    def get_beneficiary_number(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        number = (msg.text or "").strip()
        if not (number.startswith("09") and number.isdigit() and len(number) == 10):
            logging.warning(f"[COMPANY][{user_id}] رقم مستفيد غير صالح: {number}")
            bot.send_message(msg.chat.id, with_cancel_hint(f"⚠️ يا {name}، الرقم لازم يبدأ بـ 09 ويتكوّن من 10 أرقام. جرّب تاني."))
            return
        user_states[user_id]["beneficiary_number"] = number
        user_states[user_id]["step"] = "confirm_beneficiary_number"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_beneficiary_number"),
            ("✔️ تأكيد", "beneficiary_number_confirm")
        )
        logging.info(f"[COMPANY][{user_id}] رقم المستفيد: {number}")
        bot.send_message(
            msg.chat.id,
            with_cancel_hint(f"📱 تمام يا {name}، الرقم المدخّل:\n{number}\n\nنكمل؟"),
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_beneficiary_number")
    def edit_beneficiary_number(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_number"
        _replace_screen(
            bot, call,
            with_cancel_hint(f"📱 يا {name}، ابعت الرقم تاني (لازم يبدأ بـ 09):"),
            reply_markup=make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        )

    @bot.callback_query_handler(func=lambda call: call.data == "beneficiary_number_confirm")
    def beneficiary_number_confirm(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            try: bot.answer_callback_query(call.id)
            except Exception: pass
            return
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_transfer_amount"
        _replace_screen(
            bot, call,
            with_cancel_hint(f"💵 يا {name}، ابعت المبلغ اللي عايز تحوّله (مثال: 12345):"),
            reply_markup=make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        )

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_transfer_amount")
    def get_transfer_amount(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        amount_text = (msg.text or "").strip()
        try:
            amount = parse_amount(amount_text, min_value=1)
        except Exception:
            logging.warning(f"[COMPANY][{user_id}] مبلغ غير صالح: {msg.text}")
            bot.send_message(msg.chat.id, with_cancel_hint(f"⚠️ يا {name}، دخل رقم صحيح من غير فواصل أو رموز."))
            return

        commission = calculate_commission(amount)
        total = amount + commission
        user_states[user_id]["amount"] = amount
        user_states[user_id]["commission"] = commission
        user_states[user_id]["total"] = total

        user_states[user_id]["step"] = "confirming_transfer"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_transfer_amount"),
            ("✔️ تأكيد", "company_transfer_confirm")
        )
        summary = banner(
            "📤 تأكيد العملية",
            [
                f"👤 المستفيد: {user_states[user_id]['beneficiary_name']}",
                f"📱 رقم المستفيد: {user_states[user_id]['beneficiary_number']}",
                f"💸 المبلغ: {amount:,} ل.س",
                f"🧾 العمولة: {commission:,} ل.س",
                f"✅ الإجمالي: {total:,} ل.س",
                f"🏢 الشركة: {user_states[user_id]['company']}",
            ]
        )
        logging.info(f"[COMPANY][{user_id}] amount={amount}, fee={commission}, total={total}")
        bot.send_message(msg.chat.id, with_cancel_hint(f"يا {name}، راجع التفاصيل تحت وبعدين اضغط تأكيد:\n\n{summary}"), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_transfer_amount")
    def edit_transfer_amount(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_transfer_amount"
        _replace_screen(
            bot, call,
            with_cancel_hint(f"💵 تمام يا {name}، ابعت المبلغ تاني (مثال: 12345):"),
            reply_markup=make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        )

    @bot.callback_query_handler(func=lambda call: call.data == "company_transfer_confirm")
    def company_transfer_confirm(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)

        # ✅ قاعدة عامة: عند التأكيد — احذف الكيبورد فقط + Debounce
        if confirm_guard(bot, call, "company_transfer_confirm"):
            return
        _safe_delete(bot, call.message.chat.id, call.message.message_id)

        if _service_unavailable_guard(bot, call.message.chat.id):
            return

        data = user_states.get(user_id, {})
        amount = int(data.get('amount') or 0)
        commission = int(data.get('commission') or 0)
        total = int(data.get('total') or 0)
        available = get_available_balance(user_id)

        if available < total:
            shortage = total - (available or 0)
            logging.warning(f"[COMPANY][{user_id}] رصيد غير كافٍ (available={available}, total={total})")
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet"),
                ("⬅️ رجوع", "company_commission_cancel")
            )
            bot.send_message(
                call.message.chat.id,
                with_cancel_hint(
                    f"❌ يا {name}، رصيدك مش مكفي.\n"
                    f"المطلوب: {total:,} ل.س\n"
                    f"متاحك الحالي: {available:,} ل.س\n"
                    f"الناقص: {shortage:,} ل.س\n"
                    "اشحن محفظتك أو ارجع خطوة وغيّر المبلغ."
                ),
                reply_markup=kb
            )
            return

        # ✅ الهولد بدل الخصم الفوري
        hold_id = None
        try:
            reason = f"حجز حوالة شركات — {data.get('company')}"
            res = create_hold(user_id, total, reason)
            d = getattr(res, "data", None)
            if isinstance(d, dict):
                hold_id = d.get("id") or d.get("hold_id")
            elif isinstance(d, (list, tuple)) and d:
                hold_id = d[0].get("id") if isinstance(d[0], dict) else d[0]
            elif isinstance(d, (int, str)):
                hold_id = d
        except Exception as e:
            logging.exception(f"[COMPANY][{user_id}] create_hold failed: {e}")

        if not hold_id:
            bot.send_message(
                call.message.chat.id,
                with_cancel_hint("⚠️ حصلت مشكلة بسيطة وإحنا بنثبت قيمة العملية. جرّب تاني بعد شوية أو كلّمنا لو استمرت.")
            )
            return

        user_states[user_id]["step"] = "waiting_admin"

        msg = (
            f"📤 طلب حوالة مالية عبر شركات:\n"
            f"👤 المستخدم: {user_id}\n"
            f"👤 المستفيد: {data.get('beneficiary_name')}\n"
            f"📱 رقم المستفيد: {data.get('beneficiary_number')}\n"
            f"💰 المبلغ: {amount:,} ل.س\n"
            f"🏢 الشركة: {data.get('company')}\n"
            f"🧾 العمولة: {commission:,} ل.س\n"
            f"✅ الإجمالي (محجوز): {total:,} ل.س\n"
            f"🔒 HOLD: <code>{hold_id}</code>\n\n"
            f"يمكنك الرد برسالة أو صورة ليصل للعميل."
        )

        # إشعار للعميل — من غير تعديل/حذف للرسالة السابقة (الكيبورد اتشال خلاص)
        bot.send_message(
            call.message.chat.id,
            banner(f"✅ تمام يا {name}! طلبك اتبعت 🚀", ["هنراجعه بسرعة وأول ما يتنفذ هيوصلك إشعار فوري."])
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=msg,
            payload={
                "type": "companies_transfer",
                "beneficiary_name": data.get('beneficiary_name'),
                "beneficiary_number": data.get('beneficiary_number'),
                "company": data.get('company'),
                "amount": amount,
                "commission": commission,
                "total": total,
                "reserved": total,
                "hold_id": hold_id,    # ✅ أهم حاجة
            }
        )
        process_queue(bot)

    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet")
    def show_recharge_methods(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        _replace_screen(
            bot, call,
            f"💳 يا {name}، اختار طريقة شحن محفظتك:",
            reply_markup=keyboards.recharge_menu()
        )
        
    # ===== أدمن (مسارات بديلة قديمة) — مفضّلين الهولد لو موجود =====

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_company_accept_"))
    def admin_accept_company_transfer(call):
        try:
            parts = call.data.split("_")
            user_id = int(parts[-2])
            total = int(parts[-1])

            res = get_table("pending_requests").select("id, payload").eq("user_id", user_id).execute()
            if not res.data:
                bot.answer_callback_query(call.id, "❌ الطلب مش موجود.")
                return
            row = res.data[0]
            payload = row.get("payload", {}) or {}
            hold_id = payload.get("hold_id")
            reserved = int(payload.get("reserved", total) or total)
            company = payload.get("company")
            beneficiary_name = payload.get("beneficiary_name")
            beneficiary_number = payload.get("beneficiary_number")
            amount = int(payload.get("amount") or 0)

            # لو في hold صفّيه بدل خصم يدوي
            if hold_id:
                try:
                    r = capture_hold(hold_id)
                    if getattr(r, "error", None) or not bool(getattr(r, "data", True)):
                        logging.error(f"[COMPANY][ADMIN][{user_id}] capture_hold failed: {getattr(r,'error', None)}")
                        bot.answer_callback_query(call.id, "❌ مشكلة أثناء تصفية الحجز. حاول تاني.")
                        return
                except Exception as e:
                    logging.exception(f"[COMPANY][ADMIN][{user_id}] capture_hold exception: {e}")
                    bot.answer_callback_query(call.id, "❌ مشكلة أثناء تصفية الحجز. حاول تاني.")
                    return
            else:
                bot.answer_callback_query(call.id, "⚠️ لا يوجد HOLD — ارفض الطلب واطلب إعادة الإرسال.")
                bot.send_message(user_id, "⚠️ حصل تعارض بسيط. رجاءً أعد إرسال الطلب ليتم حجز المبلغ تلقائيًا.")
                return

            # سجل عملية شراء
            add_purchase(
                user_id,
                reserved,
                f"حوالة مالية عبر {company}",
                reserved,
                beneficiary_number,
            )

            # رسالة للعميل
            name = _user_name(bot, user_id)
            bot.send_message(
                user_id,
                f"✅ تمام يا {name}! تم تنفيذ حوالة مالية عبر {company} للمستفيد «{beneficiary_number}» وتم خصم {reserved:,} ل.س من محفظتك."
            )

            bot.answer_callback_query(call.id, "✅ تم القبول")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

            # رسالة/صورة اختيارية من الأدمن للعميل
            def forward_admin_message(m):
                if m.content_type == "photo":
                    file_id = m.photo[-1].file_id
                    bot.send_photo(user_id, file_id, caption=m.caption or "تمت العملية بنجاح.")
                else:
                    bot.send_message(user_id, m.text or "تمت العملية بنجاح.")
            bot.send_message(call.message.chat.id, "📝 ابعت رسالة أو صورة للعميل (اختياري).")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, forward_admin_message)

            # احذف الطلب من الطابور
            from services.queue_service import delete_pending_request
            delete_pending_request(row.get("id"))
            user_states.pop(user_id, None)
        except Exception as e:
            logging.error(f"[COMPANY][ADMIN] خطأ أثناء القبول: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ حصل خطأ: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_company_reject_"))
    def admin_reject_company_transfer(call):
        try:
            user_id = int(call.data.split("_")[-1])
            name = _user_name(bot, user_id)

            # لو فيه حجز، فُكّه
            try:
                res = get_table("pending_requests").select("id, payload").eq("user_id", user_id).execute()
                if res.data:
                    row = res.data[0]
                    payload = row.get("payload", {}) or {}
                    hold_id = payload.get("hold_id")
                    if hold_id:
                        try:
                            r = release_hold(hold_id)
                            if getattr(r, "error", None):
                                logging.error(f"[COMPANY][ADMIN][{user_id}] release_hold error: {r.error}")
                        except Exception as e:
                            logging.exception(f"[COMPANY][ADMIN][{user_id}] release_hold exception: {e}")
            except Exception:
                pass

            logging.info(f"[COMPANY][ADMIN] رفض حوالة للمستخدم {user_id}")
            def handle_reject(m):
                txt = m.text if m.content_type == "text" else "❌ تم رفض الطلب."
                if m.content_type == "photo":
                    bot.send_photo(user_id, m.photo[-1].file_id, caption=(m.caption or txt))
                else:
                    bot.send_message(user_id, f"❌ يا {name}، تم رفض الطلب من الإدارة.\n📝 السبب: {txt}")
                bot.answer_callback_query(call.id, "❌ تم الرفض")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                user_states.pop(user_id, None)
            bot.send_message(call.message.chat.id, "📝 اكتب سبب الرفض أو ابعت صورة (اختياري):")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_reject)
        except Exception as e:
            logging.error(f"[COMPANY][ADMIN] خطأ في الرفض: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ حصل خطأ: {e}")
