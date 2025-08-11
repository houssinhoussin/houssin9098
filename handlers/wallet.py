# handlers/wallet.py
from telebot import types
from config import BOT_NAME
from handlers import keyboards
from services.wallet_service import (
    get_all_purchases_structured,
    get_balance, add_balance, deduct_balance, get_purchases, get_deposit_transfers,
    has_sufficient_balance, transfer_balance,
    register_user_if_not_exist,  # ✅ تأكد من تسجيل المستخدم
    _select_single,              # للتحقق من العميل
    get_transfers,               # (موجود لو احتجته)
    get_wallet_transfers_only,   # ✅ سجل إيداع/تحويل فقط
    get_ads_purchases,
    get_bill_and_units_purchases,
    get_cash_transfer_purchases,
    get_companies_transfer_purchases,
    get_internet_providers_purchases,
    get_university_fees_purchases,
    get_wholesale_purchases,
    user_has_admin_approval,
    get_available_balance,       # ✅ المتاح = balance - held
)
from services.queue_service import add_pending_request
import logging

transfer_steps = {}

# ==== Helpers للرسائل الموحّدة ====
def _name_from_msg(msg) -> str:
    n = getattr(msg.from_user, "first_name", None) or getattr(msg.from_user, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

# ✅ عرض المحفظة
def show_wallet(bot, message, history=None):
    user_id = message.from_user.id
    name = _name_from_msg(message)
    register_user_if_not_exist(user_id, name)
    balance = get_balance(user_id)

    if history is not None:
        history.setdefault(user_id, []).append("wallet")

    text = (
        f"🧾 يا {name}، رقم حسابك: <code>{user_id}</code>\n"
        f"💰 رصيدك الحالي: <b>{_fmt_syp(balance)}</b>\n"
        f"لو محتاج أي مساعدة، إحنا معاك على طول 😉"
    )
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=keyboards.wallet_menu()
    )

# ✅ عرض المشتريات (منسّق + بلا تكرار)
def show_purchases(bot, message, history=None):
    user_id = message.from_user.id
    name = _name_from_msg(message)
    register_user_if_not_exist(user_id, name)

    items = get_all_purchases_structured(user_id, limit=50)

    if history is not None:
        history.setdefault(user_id, []).append("wallet")

    if not items:
        bot.send_message(
            message.chat.id,
            f"📦 يا {name}، لسه ما فيش مشتريات.\nاختار منتج وخلّينا نزبطك 😎",
            reply_markup=keyboards.wallet_menu()
        )
        return

    lines = []
    for it in items:
        title = it.get("title") or "منتج"
        price = int(it.get("price") or 0)
        ts    = (it.get("created_at") or "")[:19].replace("T", " ")
        suffix = f" — آيدي/رقم: {it.get('id_or_phone')}" if it.get("id_or_phone") else ""
        lines.append(f"• {title} ({price:,} ل.س) — بتاريخ {ts}{suffix}")

    lines = [ln for ln in lines if "لا توجد" not in ln]

    text = "🛍️ مشترياتك:\n" + "\n".join(lines[:50])
    bot.send_message(message.chat.id, text, reply_markup=keyboards.wallet_menu())

# ✅ سجل التحويلات (شحن محفظة + تحويل صادر فقط)
def show_transfers(bot, message, history=None):
    user_id = message.from_user.id
    name = _name_from_msg(message)
    register_user_if_not_exist(user_id, name)

    rows = get_wallet_transfers_only(user_id, limit=50)

    if history is not None:
        history.setdefault(user_id, []).append("wallet")

    if not rows:
        bot.send_message(
            message.chat.id,
            f"📄 يا {name}، ما فيش عمليات لسه.",
            reply_markup=keyboards.wallet_menu()
        )
        return

    lines = []
    for r in rows:
        desc = (r.get("description") or "").strip()
        amt  = int(r.get("amount") or 0)
        ts   = (r.get("timestamp") or "")[:19].replace("T", " ")

        if amt > 0 and (desc.startswith("إيداع") or desc.startswith("شحن")):
            lines.append(f"شحن محفظة | {amt:,} ل.س | {ts}")
            continue

        if amt < 0 and desc.startswith("تحويل إلى"):
            lines.append(f"تحويل صادر | {abs(amt):,} ل.س | {ts}")
            continue

    if not lines:
        bot.send_message(
            message.chat.id,
            f"📄 يا {name}، ما فيش عمليات لسه.",
            reply_markup=keyboards.wallet_menu()
        )
        return

    text = "📑 السجل: شحن المحفظة + تحويلاتك الصادرة\n" + "\n".join(lines)
    bot.send_message(message.chat.id, text, reply_markup=keyboards.wallet_menu())

# --- تسجيل هاندلرات الأزرار داخل register ---
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
        name = _name_from_msg(msg)
        register_user_if_not_exist(user_id, name)
        if history is not None:
            history.setdefault(user_id, []).append("wallet")
        warning = (
            f"⚠️ يا {name}، تنبيه مهم:\n"
            "الخدمة دي تحويل مباشر بين العملاء. رجاءً راجع البيانات كويس قبل التأكيد.\n\n"
            "اضغط (✅ موافق) للمتابعة أو (⬅️ رجوع) للعودة."
        )
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("✅ موافق", "⬅️ رجوع", "🔄 ابدأ من جديد")
        bot.send_message(msg.chat.id, warning, reply_markup=kb)

    @bot.message_handler(func=lambda msg: msg.text == "✅ موافق")
    def ask_for_target_id(msg):
        bot.send_message(
            msg.chat.id,
            "🔢 ابعت رقم حساب (ID) العميل المستلم:",
            reply_markup=keyboards.hide_keyboard()
        )
        transfer_steps[msg.from_user.id] = {"step": "awaiting_id"}

    @bot.message_handler(func=lambda msg: transfer_steps.get(msg.from_user.id, {}).get("step") == "awaiting_id")
    def receive_target_id(msg):
        name = _name_from_msg(msg)
        try:
            target_id = int(msg.text.strip())
        except Exception:
            bot.send_message(msg.chat.id, f"❌ يا {name}، ادخل ID صحيح لو سمحت.")
            return

        # تحقق من أنّه عميل مسجّل
        is_client = _select_single("houssin363", "user_id", target_id)
        if not is_client:
            bot.send_message(
                msg.chat.id,
                f"❌ يا {name}، الرقم ده مش لعميل مسجّل عندنا.\n"
                "الخدمة خاصة بعملاء المتجر. تقدر تدعو صاحبك للاشتراك في البوت 😉\n"
                "https://t.me/my_fast_shop_bot",
                reply_markup=keyboards.wallet_menu()
            )
            transfer_steps.pop(msg.from_user.id, None)
            return

        transfer_steps[msg.from_user.id].update({"step": "awaiting_amount", "target_id": target_id})
        bot.send_message(msg.chat.id, "💵 اكتب المبلغ اللي عايز تحوّله:")

    @bot.message_handler(func=lambda msg: transfer_steps.get(msg.from_user.id, {}).get("step") == "awaiting_amount")
    def receive_amount(msg):
        user_id = msg.from_user.id
        name = _name_from_msg(msg)
        try:
            amount = int(msg.text.strip())
        except Exception:
            bot.send_message(msg.chat.id, f"❌ يا {name}، ادخل مبلغ صحيح.")
            return

        if amount <= 0:
            bot.send_message(msg.chat.id, f"❌ يا {name}، ما ينفعش تحويل بصفر أو أقل.")
            return

        # ✅ استخدم الرصيد المتاح (يحترم الحجز)
        current_available = get_available_balance(user_id)
        min_left = 6000
        if current_available - amount < min_left:
            short = amount - (current_available - min_left)
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
            kb.add("✏️ تعديل المبلغ", "❌ إلغاء")
            bot.send_message(
                msg.chat.id,
                f"❌ آسفين يا {name}!\n"
                f"لازم يفضل في محفظتك على الأقل <b>{_fmt_syp(min_left)}</b> بعد التحويل.\n"
                f"متاحك الحالي: <b>{_fmt_syp(current_available)}</b>\n"
                f"لو عايز تحوّل {_fmt_syp(amount)}, محتاج تشحن حوالي <b>{_fmt_syp(short)}</b>.",
                parse_mode="HTML",
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
            f"📤 يا {name}، تؤكد تحويل <b>{_fmt_syp(amount)}</b> إلى الحساب <code>{target_id}</code>؟",
            parse_mode="HTML",
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: msg.text == "✏️ تعديل المبلغ")
    def edit_amount(msg):
        user_id = msg.from_user.id
        if transfer_steps.get(user_id, {}).get("step") == "awaiting_amount":
            bot.send_message(
                msg.chat.id,
                "💵 اكتب المبلغ الجديد:",
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
        name = _name_from_msg(msg)
        bot.send_message(
            msg.chat.id,
            f"تم إلغاء العملية يا {name} والرجوع للقائمة الرئيسية.",
            reply_markup=keyboards.wallet_menu()
        )
        transfer_steps.pop(user_id, None)

    @bot.message_handler(func=lambda msg: msg.text == "✅ تأكيد التحويل")
    def confirm_transfer(msg):
        user_id = msg.from_user.id
        name = _name_from_msg(msg)
        step = transfer_steps.get(user_id)
        if not step or step.get("step") != "awaiting_confirm":
            return

        amount    = step["amount"]
        target_id = step["target_id"]

        # تأكيد وجود المرسل
        register_user_if_not_exist(user_id, msg.from_user.full_name)

        # ✅ تنفيذ التحويل مباشرة بين العملاء (آمن عبر RPC ويحترم المتاح)
        success = transfer_balance(user_id, target_id, amount)
        if not success:
            bot.send_message(msg.chat.id, f"❌ يا {name}، فشل التحويل. راجع رصيدك وجرب تاني.")
            return

        # رسالة للمرسِل بتفاصيل واضحة (موحّدة الأسلوب)
        bot.send_message(
            msg.chat.id,
            f"✅ تمام يا {name}! تم تحويل <b>{_fmt_syp(amount)}</b> إلى الحساب <code>{target_id}</code> "
            f"وتم خصم <b>{_fmt_syp(amount)}</b> من محفظتك 🎉",
            parse_mode="HTML",
            reply_markup=keyboards.wallet_menu()
        )

        # إشعار المستلم بالتعبئة ومن أي حساب
        try:
            sender_name = msg.from_user.full_name
            bot.send_message(
                target_id,
                f"💰 يا {sender_name} بعتلك <b>{_fmt_syp(amount)}</b> على محفظتك (من الحساب <code>{user_id}</code>).\n"
                f"استخدمها براحتك 😉",
                parse_mode="HTML",
                reply_markup=keyboards.wallet_menu()
            )
        except Exception:
            pass

        transfer_steps.pop(user_id, None)
        show_wallet(bot, msg, history)
