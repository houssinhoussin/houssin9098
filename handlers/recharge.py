# -*- coding: utf-8 -*-
from telebot import types
from config import ADMIN_MAIN_ID
from services.recharge_service import apply_recharge
from handlers import keyboards  # ✅ الكيبورد الموحد
from services.wallet_service import register_user_if_not_exist, get_balance
from types import SimpleNamespace
from services.queue_service import add_pending_request, process_queue
from services.validators import parse_amount
from services.telegram_safety import remove_inline_keyboard
from services.anti_spam import too_soon
from services.feature_flags import require_feature_or_alert
import logging

# NEW: بنفحص الطابور الفعلي
from database.db import get_table

# حارس التأكيد الموحّد: يحذف الكيبورد + يعمل Debounce
try:
    from services.ui_guards import confirm_guard
except Exception:
    from ui_guards import confirm_guard

recharge_requests = {}
recharge_pending = set()

# ✅ الحد الأدنى للشحن
MIN_RECHARGE = 15000

SYRIATEL_NUMBERS = ["0011111", "0022222", "0033333", "0044444"]
# 🔧 إصلاح تكرار في القائمة
MTN_NUMBERS = ["0005555", "0006666", "0007777"]
SHAMCASH_CODES = ["000xz55XH55", "00YI06MB666"]
PAYEER_CODES = ["0PPWY0777JG7"]

# ==== Helpers ====
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."
ETA_TEXT = "من 1 إلى 4 دقائق"

def _name_from_user(u) -> str:
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _with_cancel(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _card(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def get_method_instructions(method):
    if method == "سيرياتيل كاش":
        text = (
            "📲 *سيرياتيل كاش*\n"
            "حوّل المبلغ إلى أحد الأرقام التالية عبر (الدفع اليدوي):\n"
            f"🔢 {'   -   '.join(f'`{num}`' for num in SYRIATEL_NUMBERS)}\n"
            "⚠️ لسنا مسؤولين عن تحويل الوحدات (اتّبع التعليمات بدقة)\n\n"
            "يمكنك نسخ الرقم المطلوب بسهولة."
        )
    elif method == "أم تي إن كاش":
        text = (
            "📲 *أم تي إن كاش*\n"
            "حوّل المبلغ إلى أحد الأرقام التالية عبر (الدفع اليدوي):\n"
            f"🔢 {'   -   '.join(f'`{num}`' for num in MTN_NUMBERS)}\n"
            "⚠️ لسنا مسؤولين عن تحويل الوحدات (اتّبع التعليمات بدقة)\n\n"
            "يمكنك نسخ الرقم المطلوب بسهولة."
        )
    elif method == "شام كاش":
        text = (
            "📲 *شام كاش*\n"
            "حوّل المبلغ إلى أحد الأكواد التالية:\n"
            f"🔢 {'   -   '.join(f'`{code}`' for code in SHAMCASH_CODES)}\n"
            "يمكنك نسخ الكود المطلوب بسهولة."
        )
    elif method == "Payeer":
        text = (
            "💳 *Payeer*\n"
            "حوّل المبلغ إلى الكود التالي:\n"
            f"🔢 {'   -   '.join(f'`{code}`' for code in PAYEER_CODES)}\n"
            "يمكنك نسخ الكود بسهولة."
        )
    else:
        text = "حدث خطأ في تحديد طريقة الشحن."
    return text

def clear_pending_request(user_id):
    """تُنادى من لوحة الأدمن بعد القبول/الإلغاء لتنظيف قفل الشحن المحلي."""
    recharge_pending.discard(user_id)
    recharge_requests.pop(user_id, None)

# NEW: هل لدى المستخدم طلب شحن مفتوح فعليًا في الطابور؟
def has_open_recharge(user_id: int) -> bool:
    try:
        res = (
            get_table("pending_requests")
            .select("id, payload")
            .eq("user_id", user_id)
            .execute()
        )
        for row in (res.data or []):
            typ = (row.get("payload") or {}).get("type")
            if typ in ("recharge", "wallet_recharge", "deposit"):
                return True
    except Exception as e:
        logging.exception("[RECHARGE] has_open_recharge failed: %s", e)
    return False

# NEW: تنظيف ذاتي لو set فيها بقايا قديمة
def _heal_local_lock(user_id: int):
    if user_id in recharge_pending and not has_open_recharge(user_id):
        recharge_pending.discard(user_id)

def start_recharge_menu(bot, message, history=None):
    uid = message.from_user.id

    # ✅ تطبيع history[uid] ليكون دائمًا قائمة قبل أي append
    if history is not None:
        current = history.get(uid)
        if isinstance(current, list):
            pass
        elif current is None:
            history[uid] = []
        elif isinstance(current, str):
            history[uid] = [current]
        else:
            history[uid] = []
        history[uid].append("recharge_menu")

    name = _name_from_user(message.from_user)
    logging.info(f"[RECHARGE][{uid}] فتح قائمة الشحن")
    # ميزة "شحن المحفظة" مقفولة؟ أرسل اعتذار وانهِ الدالة
    if require_feature_or_alert(bot, message.chat.id, "wallet_recharge", "شحن المحفظة"):
        return
    bot.send_message(
        message.chat.id,
        _with_cancel(f"💳 يا {name}، اختار طريقة شحن محفظتك:"),
        reply_markup=keyboards.recharge_menu()
    )

def register(bot, history):

    # /cancel — إلغاء سريع من أي خطوة
    @bot.message_handler(commands=['cancel'])
    def _cancel_all(msg):
        uid = msg.from_user.id
        clear_pending_request(uid)
        name = _name_from_user(msg.from_user)
        bot.send_message(
            msg.chat.id,
            _card("✅ تم الإلغاء", [f"يا {name}، رجعناك لشاشة الشحن."]),
            reply_markup=keyboards.recharge_menu()
        )

    @bot.message_handler(func=lambda msg: msg.text == "💳 شحن محفظتي")
    def open_recharge(msg):
        start_recharge_menu(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text in [
        "📲 سيرياتيل كاش", "📲 أم تي إن كاش", "📲 شام كاش", "💳 Payeer"
    ])
    def request_invoice(msg):
        user_id = msg.from_user.id
        name = _name_from_user(msg.from_user)

        # NEW: شيل القفل المحلي لو مفيش طلب فعلي مفتوح
        _heal_local_lock(user_id)

        # NEW: اسمح بالبدء فقط لو مفيش طلب شحن فعلي مفتوح ولا قفل محلي
        if user_id in recharge_pending or has_open_recharge(user_id):
            logging.warning(f"[RECHARGE][{user_id}] محاولة شحن جديدة أثناء وجود طلب معلق")
            bot.send_message(msg.chat.id, _with_cancel(f"⚠️ يا {name}، عندك طلب شحن قيد المعالجة. استنى شوية لو سمحت."))
            return

        method = msg.text.replace("📲 ", "").replace("💳 ", "")
        feature_map = {
            "سيرياتيل كاش": "recharge_syriatel",
            "أم تي إن كاش": "recharge_mtn",
            "شام كاش": "recharge_sham",
            "Payeer": "recharge_payeer",
        }
        fk = feature_map.get(method)
        # لو الطريقة مقفولة، أرسل الاعتذار الاحترافي وتوقّف
        if fk and require_feature_or_alert(bot, msg.chat.id, fk, f"شحن — {method}"):
            return

        recharge_requests[user_id] = {"method": method}
        instructions = get_method_instructions(method)
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد التحويل", callback_data="confirm_recharge_method"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_recharge_method")
        )
        logging.info(f"[RECHARGE][{user_id}] بدأ شحن بطريقة: {method}")
        bot.send_message(
            msg.chat.id,
            _with_cancel(instructions),
            parse_mode="Markdown",
            reply_markup=markup
        )

    # دعم نداء عام لعرض طرق الشحن من أي شاشة (يستخدمه بعض المسارات)
    @bot.callback_query_handler(func=lambda c: c.data == "show_recharge_methods")
    def _show_recharge_methods_from_anywhere(call):
        try:
            bot.send_message(call.message.chat.id, "💳 اختار طريقة شحن محفظتك:", reply_markup=keyboards.recharge_menu())
        except Exception:
            bot.send_message(call.message.chat.id, "💳 لعرض طرق الشحن، افتح قائمة الشحن من الرئيسية.")
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda call: call.data in ["confirm_recharge_method", "cancel_recharge_method"])
    def handle_method_confirm_cancel(call):
        user_id = call.from_user.id
        # احذف الكيبورد الأول علشان نتجنب 400 message is not modified
        try:
            remove_inline_keyboard(bot, call.message)
        except Exception:
            pass

        if too_soon(user_id, 'handle_method_confirm_cancel', seconds=2):
            try:
                return bot.answer_callback_query(call.id, '⏱️ تم الاستلام..')
            except Exception:
                return

        name = _name_from_user(call.from_user)
        if call.data == "confirm_recharge_method":
            method = (recharge_requests.get(user_id) or {}).get("method")
            feature_map = {
                "سيرياتيل كاش": "recharge_syriatel",
                "أم تي إن كاش": "recharge_mtn",
                "شام كاش": "recharge_sham",
                "Payeer": "recharge_payeer",
            }
            fk = feature_map.get(method)
            if fk and require_feature_or_alert(bot, call.message.chat.id, fk, f"شحن — {method}"):
                return

            logging.info(f"[RECHARGE][{user_id}] أكد طريقة الشحن، بانتظار الصورة")
            bot.send_message(
                call.message.chat.id,
                _with_cancel(f"📸 يا {name}، ابعت صورة إشعار الدفع (سكرين/لقطة شاشة):"),
                reply_markup=keyboards.recharge_menu()
            )
        else:
            clear_pending_request(user_id)
            logging.info(f"[RECHARGE][{user_id}] ألغى الشحن من شاشة اختيار الطريقة")
            bot.send_message(
                call.message.chat.id,
                _with_cancel(f"❌ تم الإلغاء يا {name}. تقدر تبدأ من جديد في أي وقت."),
                reply_markup=keyboards.recharge_menu()
            )

    @bot.message_handler(content_types=["photo"])
    def handle_photo(msg):
        user_id = msg.from_user.id
        if user_id not in recharge_requests or "photo" in recharge_requests[user_id]:
            return
        # Anti-spam بسيط
        if too_soon(user_id, 'recharge_photo', seconds=1):
            return
        photo_id = msg.photo[-1].file_id
        recharge_requests[user_id]["photo"] = photo_id
        name = _name_from_user(msg.from_user)
        logging.info(f"[RECHARGE][{user_id}] أرسل صورة إشعار الدفع")
        bot.send_message(msg.chat.id, _with_cancel(f"🔢 تمام يا {name}! ابعت رقم الإشعار / رمز العملية:"), reply_markup=keyboards.recharge_menu())

    @bot.message_handler(
        func=lambda msg: msg.from_user.id in recharge_requests 
        and "photo" in recharge_requests[msg.from_user.id] 
        and "ref" not in recharge_requests[msg.from_user.id]
    )
    def get_reference(msg):
        user_id = msg.from_user.id
        # Anti-spam خفيف
        if too_soon(user_id, 'recharge_ref', seconds=1):
            return
        recharge_requests[user_id]["ref"] = (msg.text or "").strip()
        logging.info(f"[RECHARGE][{user_id}] أرسل رقم الإشعار: {msg.text}")
        bot.send_message(msg.chat.id, _with_cancel("💰 ابعت مبلغ الشحن (بالليرة السورية):"), reply_markup=keyboards.recharge_menu())

    @bot.message_handler(
        func=lambda msg: msg.from_user.id in recharge_requests 
        and "ref" in recharge_requests[msg.from_user.id] 
        and "amount" not in recharge_requests[msg.from_user.id]
    )
    def get_amount(msg):
        user_id = msg.from_user.id
        name = _name_from_user(msg.from_user)
        amount_text = (msg.text or "").strip()

        # ✅ validator الآمن — استخدم الوسيط الصحيح min_value
        try:
            amount = parse_amount(amount_text, min_value=MIN_RECHARGE)
        except Exception:
            logging.warning(f"[RECHARGE][{user_id}] محاولة إدخال مبلغ شحن غير صالح: {amount_text}")
            bot.send_message(
                msg.chat.id,
                _with_cancel(f"❌ يا {name}، دخّل المبلغ أرقام فقط (من غير فواصل/نقاط/رموز)."),
                reply_markup=keyboards.recharge_menu()
            )
            return

        if amount < MIN_RECHARGE:
            bot.send_message(
                msg.chat.id,
                _with_cancel(
                    f"⚠️ يا {name}، الحد الأدنى للشحن هو <b>{_fmt_syp(MIN_RECHARGE)}</b>.\n"
                    f"اكتب مبلغ أكبر أو يساويه، وبنبقى ننفّذ طلبك {ETA_TEXT}."
                ),
                parse_mode="HTML",
                reply_markup=keyboards.recharge_menu()
            )
            return

        data = recharge_requests[user_id]
        data["amount"] = int(amount)

        confirm_text = (
            "🔎 **راجع تفاصيل طلب الشحن:**\n"
            f"💳 الطريقة: {data['method']}\n"
            f"🔢 رقم الإشعار: `{data['ref']}`\n"
            f"💵 المبلغ: {amount:,} ل.س\n\n"
            f"لو كل حاجة تمام، ابعت الطلب للإدارة.\n\n"
            f"{CANCEL_HINT}"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد", callback_data="user_confirm_recharge"),
            types.InlineKeyboardButton("🔁 تعديل", callback_data="user_edit_recharge"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="user_cancel_recharge")
        )

        logging.info(f"[RECHARGE][{user_id}] تأكيد معلومات الشحن: مبلغ {amount}")
        photo_id = data.get("photo")
        if photo_id:
            bot.send_photo(
                msg.chat.id,
                photo_id,
                caption=confirm_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
        else:
            bot.send_message(
                msg.chat.id,
                confirm_text,
                parse_mode="Markdown",
                reply_markup=markup
            )

    @bot.callback_query_handler(
        func=lambda call: call.data in ["user_confirm_recharge", "user_edit_recharge", "user_cancel_recharge"]
    )
    def handle_user_recharge_action(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)

        if call.data == "user_confirm_recharge":
            # ✅ احذف الكيبورد + امنع الدبل-كليك (موحّد)
            if confirm_guard(bot, call, "user_confirm_recharge"):
                return

            data = recharge_requests.get(user_id)
            if not data:
                logging.warning(f"[RECHARGE][{user_id}] تأكيد طلب شحن بدون بيانات")
                try:
                    bot.answer_callback_query(call.id, "لا يوجد طلب قيد المعالجة.")
                except Exception:
                    pass
                return

            amount = int(data.get("amount") or 0)
            if amount < MIN_RECHARGE:
                recharge_requests[user_id].pop("amount", None)
                try:
                    bot.answer_callback_query(call.id, "المبلغ أقل من الحد الأدنى.")
                except Exception:
                    pass
                bot.send_message(
                    user_id,
                    _with_cancel(
                        f"⚠️ يا {name}، الحد الأدنى للشحن هو <b>{_fmt_syp(MIN_RECHARGE)}</b>.\n"
                        f"من فضلك ادخل مبلغ جديد أكبر أو يساويه."
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboards.recharge_menu()
                )
                return

            # ✅ تأكيد التسجيل (للإظهار في رسالة الأدمن)
            register_user_if_not_exist(user_id, name)
            try:
                balance = int(get_balance(user_id))
            except Exception:
                balance = 0

            # ===== رسالة الأدمن بالقالب الموحّد =====
            admin_msg = (
                f"💰 رصيد المستخدم: {balance:,} ل.س\n"
                f"🆕 طلب جديد\n"
                f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
                f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
                f"آيدي: <code>{user_id}</code>\n"
                f"آيدي اللاعب: <code>—</code>\n"
                f"🔖 المنتج: شحن محفظة\n"
                f"التصنيف: محفظة\n"
                f"💵 السعر: {amount:,} ل.س\n"
                f"(recharge)"
            )
            admin_msg += (
                f"\n\n"
                f"🔢 رقم الإشعار: <code>{data['ref']}</code>\n"
                f"💳 الطريقة: <code>{data['method']}</code>"
            )

            logging.info(f"[RECHARGE][{user_id}] إرسال طلب الشحن للإدارة")
            add_pending_request(
                user_id=user_id,
                username=call.from_user.username,
                request_text=admin_msg,
                payload={
                    "type": "recharge",
                    "amount": amount,
                    "method": data['method'],
                    "ref": data['ref'],
                    "photo": data.get("photo"),
                }
            )
            process_queue(bot)

            # ===== رسالة موحّدة للعميل =====
            bot.send_message(
                user_id,
                _with_cancel(
                    f"✅ تمام يا {name}! استلمنا طلب شحن محفظتك بقيمة <b>{_fmt_syp(amount)}</b>.\n"
                    f"⏱️ سيتم تنفيذ الطلب {ETA_TEXT}.\n"
                    f"لو في أي ملاحظة هنبعتلك فورًا 💬"
                ),
                parse_mode="HTML",
                reply_markup=keyboards.recharge_menu()
            )
            recharge_pending.add(user_id)

        elif call.data == "user_edit_recharge":
            if user_id in recharge_requests:
                recharge_requests[user_id].pop("amount", None)
                recharge_requests[user_id].pop("ref", None)
                logging.info(f"[RECHARGE][{user_id}] تعديل طلب الشحن")
                bot.send_message(
                    user_id,
                    _with_cancel("🔄 ابعت رقم الإشعار / رمز العملية من جديد:"),
                    reply_markup=keyboards.recharge_menu()
                )
            # 🧹 إزالة الكيبورد بأمان
            try:
                remove_inline_keyboard(bot, call.message)
            except Exception:
                pass

        elif call.data == "user_cancel_recharge":
            clear_pending_request(user_id)
            logging.info(f"[RECHARGE][{user_id}] ألغى طلب الشحن نهائياً")
            bot.send_message(
                user_id,
                _with_cancel(f"❌ تم إلغاء الطلب يا {name}. تقدر تبدأ من جديد وقت ما تحب."),
                reply_markup=keyboards.recharge_menu()
            )
            # تصحيح history قبل استدعاء start_recharge_menu
            if not isinstance(history.get(user_id), list):
                history[user_id] = []

            fake_msg = SimpleNamespace()
            fake_msg.from_user = SimpleNamespace()
            fake_msg.from_user.id = user_id
            fake_msg.from_user.first_name = name
            fake_msg.chat = SimpleNamespace()
            fake_msg.chat.id = user_id

            start_recharge_menu(bot, fake_msg, history)
            # 🧹 إزالة الكيبورد بأمان
            try:
                remove_inline_keyboard(bot, call.message)
            except Exception:
                pass
