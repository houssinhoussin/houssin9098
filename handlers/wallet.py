from telebot import types
from config import BOT_NAME
from handlers import keyboards
from services.wallet_service import ( get_all_purchases_structured,
    get_balance, add_balance, deduct_balance, get_purchases, get_deposit_transfers,
    has_sufficient_balance, transfer_balance, get_table,
    register_user_if_not_exist,  # ✅ الاستيراد الصحيح
    _select_single,  # لاستعماله في التحقق من العميل
    get_transfers,   # ✅ الاستيراد الصحيح الجديد
    get_wallet_transfers_only,
)
from services.wallet_service import ( get_all_purchases_structured,
    get_ads_purchases,
    get_bill_and_units_purchases,
    get_cash_transfer_purchases,
    get_companies_transfer_purchases,
    get_internet_providers_purchases,
    get_university_fees_purchases,
    get_wholesale_purchases,
    user_has_admin_approval  # إضافة جديدة للتحقق من موافقة الادمن
)

from services.queue_service import add_pending_request
import logging

transfer_steps = {}

# ✅ عرض المحفظة
def show_wallet(bot, message, history=None):
    user_id = message.from_user.id
    name = message.from_user.full_name
    register_user_if_not_exist(user_id, name)  # تأكد من تسجيل المستخدم
    balance = get_balance(user_id)

    if history is not None:
        history.setdefault(user_id, []).append("wallet")

    text = (
        f"🧾 رقم حسابك: `{user_id}`\n"
        f"💰 رصيدك الحالي: {balance:,} ل.س"
    )
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=keyboards.wallet_menu()
    )

# ✅ عرض المشتريات (منسّق + بدون تكرار)
def show_purchases(bot, message, history=None):
    user_id = message.from_user.id
    name = message.from_user.full_name
    register_user_if_not_exist(user_id, name)

    items = get_all_purchases_structured(user_id, limit=50)

    if history is not None:
        history.setdefault(user_id, []).append("wallet")

    if not items:
        return bot.send_message(
            message.chat.id,
            "📦 لا توجد مشتريات حتى الآن.",
            reply_markup=keyboards.wallet_menu()
        )

    lines = []
    for it in items:
        title = it.get("title") or "منتج"
        price = int(it.get("price") or 0)
        ts    = (it.get("created_at") or "")[:19].replace("T", " ")
        suffix = f" — آيدي/رقم: {it.get('id_or_phone')}" if it.get("id_or_phone") else ""
        lines.append(f"• {title} ({price:,} ل.س) — بتاريخ {ts}{suffix}")

    # حذف أي أسطر افتراضية تحتوي "لا توجد ..."
    lines = [ln for ln in lines if "لا توجد" not in ln]

    text = "🛍️ مشترياتك:\n" + "\n".join(lines[:50])
    bot.send_message(message.chat.id, text, reply_markup=keyboards.wallet_menu())

# ✅ سجل التحويلات (إيداعات + تحويلات فقط)
def show_transfers(bot, message, history=None):
    user_id = message.from_user.id
    name = message.from_user.full_name
    register_user_if_not_exist(user_id, name)

    rows = get_wallet_transfers_only(user_id, limit=50)

    if history is not None:
        history.setdefault(user_id, []).append("wallet")

    if not rows:
        return bot.send_message(
            message.chat.id,
            "📄 لا توجد عمليات إيداع/تحويل حتى الآن.",
            reply_markup=keyboards.wallet_menu()
        )

    lines = []
    for r in rows:
        ts = (r.get("timestamp") or "")[:19].replace("T", " ")
        lines.append(f"{r['description']} ({r['amount']:+,} ل.س) في {ts}")
    text = "📑 سجل التحويلات:\n" + "\n".join(lines)
    bot.send_message(message.chat.id, text, reply_markup=keyboards.wallet_menu())

# --- تسجيل هاندلرات الأزرار داخل register (وجود bot) ---
def register(bot, history=None):
    @bot.message_handler(func=lambda msg: msg.text == "💰 محفظتي")
    def handle_wallet(msg):
        show_wallet(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text == "🛍️ مشترياتي")
    def handle_purchases(msg):
        show_purchases(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text == "📑 سجل التحويلات")
    def handle_transfers(msg):
        show_transfers(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text == "🔁 تحويل من محفظتك إلى محفظة عميل آخر")
    def handle_transfer_notice(msg):
        user_id = msg.from_user.id
        name = msg.from_user.full_name
        register_user_if_not_exist(user_id, name)
        if history is not None:
            history.setdefault(user_id, []).append("wallet")
        warning = (
            "⚠️ تنويه:\n"
            "هذه العملية خاصة بين المستخدمين فقط.\n"
            "لسنا مسؤولين عن أي خطأ يحدث عند تحويلك رصيدًا لعميل آخر.\n"
            "اتبع التعليمات جيدًا.\n\n"
            "اضغط (✅ موافق) للمتابعة أو (⬅️ رجوع) للعودة."
        )
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("✅ موافق", "⬅️ رجوع", "🔄 ابدأ من جديد")
        bot.send_message(msg.chat.id, warning, reply_markup=kb)

    @bot.message_handler(func=lambda msg: msg.text == "✅ موافق")
    def ask_for_target_id(msg):
        bot.send_message(
            msg.chat.id,
            "🔢 أدخل رقم ID الخاص بالعميل (رقم الحساب):",
            reply_markup=keyboards.hide_keyboard()
        )
        transfer_steps[msg.from_user.id] = {"step": "awaiting_id"}

    @bot.message_handler(func=lambda msg: transfer_steps.get(msg.from_user.id, {}).get("step") == "awaiting_id")
    def receive_target_id(msg):
        try:
            target_id = int(msg.text.strip())
        except:
            bot.send_message(msg.chat.id, "❌ الرجاء إدخال رقم ID صحيح.")
            return
        is_client = _select_single("houssin363", "user_id", target_id)
        if not is_client:
            bot.send_message(
                msg.chat.id,
                "❌ هذا الرقم ليس من عملائنا. هذه الخدمة خاصة بعملاء المتجر فقط.\n"
                "يمكنك دعوة العميل للاشتراك في البوت:\n"
                "https://t.me/my_fast_shop_bot",
                reply_markup=keyboards.wallet_menu()
            )
            transfer_steps.pop(msg.from_user.id, None)
            return
        transfer_steps[msg.from_user.id].update({"step": "awaiting_amount", "target_id": target_id})
        bot.send_message(msg.chat.id, "💵 أدخل المبلغ الذي تريد تحويله:")

    @bot.message_handler(func=lambda msg: transfer_steps.get(msg.from_user.id, {}).get("step") == "awaiting_amount")
    def receive_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text.strip())
        except:
            bot.send_message(msg.chat.id, "❌ الرجاء إدخال مبلغ صالح.")
            return
        if amount <= 0:
            bot.send_message(msg.chat.id, "❌ لا يمكن تحويل مبلغ صفر أو أقل.")
            return

        current_balance = get_balance(user_id)
        min_left = 6000
        if current_balance - amount < min_left:
            short = amount - (current_balance - min_left)
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
            kb.add("✏️ تعديل المبلغ", "❌ إلغاء")
            bot.send_message(
                msg.chat.id,
                f"❌ طلبك مرفوض!\n"
                f"لا يمكن أن يقل الرصيد عن {min_left:,} ل.س بعد التحويل.\n"
                f"لتحويل {amount:,} ل.س، يجب شحن محفظتك بمبلغ لا يقل عن {short:,} ل.س.",
                reply_markup=kb
            )
            transfer_steps[user_id]["step"] = "awaiting_amount"
            return

        target_id = transfer_steps[user_id]["target_id"]
        transfer_steps[user_id].update({"step": "awaiting_confirm", "amount": amount})
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("✅ تأكيد التحويل", "⬅️ رجوع", "🔄 ابدأ من جديد")
        bot.send_message(
            msg.chat.id,
            f"📤 هل أنت متأكد من تحويل `{amount:,} ل.س` إلى الحساب `{target_id}`؟",
            parse_mode="Markdown",
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: msg.text == "✏️ تعديل المبلغ")
    def edit_amount(msg):
        user_id = msg.from_user.id
        if transfer_steps.get(user_id, {}).get("step") == "awaiting_amount":
            bot.send_message(
                msg.chat.id,
                "💵 أدخل المبلغ الجديد الذي تريد تحويله:",
                reply_markup=keyboards.hide_keyboard()
            )
        else:
            bot.send_message(
                msg.chat.id,
                "تم إلغاء العملية.",
                reply_markup=keyboards.wallet_menu()
            )
            transfer_steps.pop(user_id, None)

    @bot.message_handler(func=lambda msg: msg.text == "❌ إلغاء")
    def cancel_transfer(msg):
        user_id = msg.from_user.id
        bot.send_message(
            msg.chat.id,
            "تم إلغاء العملية والرجوع إلى القائمة الرئيسية.",
            reply_markup=keyboards.wallet_menu()
        )
        transfer_steps.pop(user_id, None)

    @bot.message_handler(func=lambda msg: msg.text == "✅ تأكيد التحويل")
    def confirm_transfer(msg):
        user_id = msg.from_user.id
        step = transfer_steps.get(user_id)
        if not step or step.get("step") != "awaiting_confirm":
            return
        amount = step["amount"]
        target_id = step["target_id"]
        name = msg.from_user.full_name
        register_user_if_not_exist(user_id, name)
        success = transfer_balance(user_id, target_id, amount)
        if not success:
            bot.send_message(msg.chat.id, "❌ فشل التحويل. تحقق من الرصيد والمحفظة.")
            return

        bot.send_message(
            msg.chat.id,
            "✅ تم تحويل المبلغ بنجاح.",
            reply_markup=keyboards.wallet_menu()
        )
        try:
            sender_name = msg.from_user.full_name
            bot.send_message(
                target_id,
                f"💰 تم شحن محفظتك من محفظة {sender_name} بمبلغ قدره {amount:,} ل.س.",
                reply_markup=keyboards.wallet_menu()
            )
        except Exception:
            pass
        transfer_steps.pop(user_id, None)
        show_wallet(bot, msg, history)
