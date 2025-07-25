from telebot import types
from config import ADMIN_MAIN_ID
from services.recharge_service import apply_recharge
from handlers import keyboards  # ✅ الكيبورد الموحد
from services.wallet_service import register_user_if_not_exist  # ✅ الاستيراد الجديد
from types import SimpleNamespace  # 🔴 التصحيح هنا
from services.queue_service import add_pending_request
import logging

recharge_requests = {}
recharge_pending = set()

SYRIATEL_NUMBERS = ["0011111", "0022222", "0033333", "0044444"]
MTN_NUMBERS = ["0005555", "0006666", "0006666", "0007777"]
SHAMCASH_CODES = ["000xz55XH55", "00YI06MB666"]
PAYEER_CODES = ["0PPWY0777JG7"]

def get_method_instructions(method):
    if method == "سيرياتيل كاش":
        text = (
            "📲 *سيرياتيل كاش*\n"
            "حول المبلغ إلى أحد الأرقام التالية عبر (الدفع اليدوي):\n"
            f"🔢 {'   -   '.join(f'`{num}`' for num in SYRIATEL_NUMBERS)}\n"
            "⚠️ لسنا مسؤولين عن تحويل الوحدات (انتبه للتعليمات)\n\n"
            "يمكنك نسخ الرقم المطلوب بسهولة."
        )
    elif method == "أم تي إن كاش":
        text = (
            "📲 *أم تي إن كاش*\n"
            "حول المبلغ إلى أحد الأرقام التالية عبر (الدفع اليدوي):\n"
            f"🔢 {'   -   '.join(f'`{num}`' for num in MTN_NUMBERS)}\n"
            "⚠️ لسنا مسؤولين عن تحويل الوحدات (انتبه للتعليمات)\n\n"
            "يمكنك نسخ الرقم المطلوب بسهولة."
        )
    elif method == "شام كاش":
        text = (
            "📲 *شام كاش*\n"
            "حول المبلغ إلى أحد الأكواد التالية:\n"
            f"🔢 {'   -   '.join(f'`{code}`' for code in SHAMCASH_CODES)}\n"
            "يمكنك نسخ الكود المطلوب بسهولة."
        )
    elif method == "Payeer":
        text = (
            "💳 *Payeer*\n"
            "حول المبلغ إلى الكود التالي:\n"
            f"🔢 {'   -   '.join(f'`{code}`' for code in PAYEER_CODES)}\n"
            "يمكنك نسخ الكود بسهولة."
        )
    else:
        text = "حدث خطأ في تحديد طريقة الشحن."
    return text

def clear_pending_request(user_id):
    recharge_pending.discard(user_id)
    recharge_requests.pop(user_id, None)

def start_recharge_menu(bot, message, history=None):
    if history is not None:
        # تصحيح نوع history ليكون دائماً قائمة (list)
        if not isinstance(history.get(message.from_user.id), list):
            history[message.from_user.id] = []
        history[message.from_user.id].append("recharge_menu")
    logging.info(f"[RECHARGE][{message.from_user.id}] فتح قائمة الشحن")
    bot.send_message(
        message.chat.id,
        "💳 اختر طريقة شحن محفظتك:",
        reply_markup=keyboards.recharge_menu()
    )

def register(bot, history):

    @bot.message_handler(func=lambda msg: msg.text == "💳 شحن محفظتي")
    def open_recharge(msg):
        start_recharge_menu(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text in [
        "📲 سيرياتيل كاش", "📲 أم تي إن كاش", "📲 شام كاش", "💳 Payeer"
    ])
    def request_invoice(msg):
        user_id = msg.from_user.id
        if user_id in recharge_pending:
            logging.warning(f"[RECHARGE][{user_id}] محاولة شحن جديدة أثناء وجود طلب معلق")
            bot.send_message(msg.chat.id, "⚠️ لديك طلب قيد المعالجة. الرجاء الانتظار.")
            return

        method = msg.text.replace("📲 ", "").replace("💳 ", "")
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
            instructions,
            parse_mode="Markdown",
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data in ["confirm_recharge_method", "cancel_recharge_method"])
    def handle_method_confirm_cancel(call):
        user_id = call.from_user.id
        if call.data == "confirm_recharge_method":
            logging.info(f"[RECHARGE][{user_id}] أكد طريقة الشحن، بانتظار الصورة")
            bot.send_message(
                call.message.chat.id,
                "📸 أرسل صورة إشعار الدفع (سكرين أو لقطة شاشة):",
                reply_markup=keyboards.recharge_menu()
            )
        else:
            clear_pending_request(user_id)
            logging.info(f"[RECHARGE][{user_id}] ألغى الشحن من شاشة اختيار الطريقة")
            bot.send_message(
                call.message.chat.id,
                "❌ تم إلغاء العملية. يمكنك البدء من جديد.",
                reply_markup=keyboards.recharge_menu()
            )

    @bot.message_handler(content_types=["photo"])
    def handle_photo(msg):
        user_id = msg.from_user.id
        if user_id not in recharge_requests or "photo" in recharge_requests[user_id]:
            return
        photo_id = msg.photo[-1].file_id
        recharge_requests[user_id]["photo"] = photo_id
        logging.info(f"[RECHARGE][{user_id}] أرسل صورة إشعار الدفع")
        bot.send_message(msg.chat.id, "🔢 أرسل رقم الإشعار / رمز العملية:", reply_markup=keyboards.recharge_menu())

    @bot.message_handler(
        func=lambda msg: msg.from_user.id in recharge_requests 
        and "photo" in recharge_requests[msg.from_user.id] 
        and "ref" not in recharge_requests[msg.from_user.id]
    )
    def get_reference(msg):
        recharge_requests[msg.from_user.id]["ref"] = msg.text
        logging.info(f"[RECHARGE][{msg.from_user.id}] أرسل رقم الإشعار: {msg.text}")
        bot.send_message(msg.chat.id, "💰 أرسل مبلغ الشحن (بالليرة السورية):", reply_markup=keyboards.recharge_menu())

    @bot.message_handler(
        func=lambda msg: msg.from_user.id in recharge_requests 
        and "ref" in recharge_requests[msg.from_user.id] 
        and "amount" not in recharge_requests[msg.from_user.id]
    )
    def get_amount(msg):
        user_id = msg.from_user.id
        amount_text = msg.text.strip()

        if not amount_text.isdigit():
            logging.warning(f"[RECHARGE][{user_id}] محاولة إدخال مبلغ شحن غير صالح: {amount_text}")
            bot.send_message(
                msg.chat.id,
                "❌ يرجى إدخال مبلغ صحيح بالأرقام فقط (بدون أي فواصل أو نقاط أو رموز).",
                reply_markup=keyboards.recharge_menu()
            )
            return

        amount = int(amount_text)
        data = recharge_requests[user_id]
        data["amount"] = amount

        confirm_text = (
            f"🔎 **يرجى التأكد من معلومات الشحن:**\n"
            f"💳 الطريقة: {data['method']}\n"
            f"🔢 رقم الإشعار: `{data['ref']}`\n"
            f"💵 المبلغ: {amount:,} ل.س\n\n"
            f"هل أنت متأكد من إرسال هذا الطلب للإدارة؟"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد", callback_data="user_confirm_recharge"),
            types.InlineKeyboardButton("🔁 تعديل", callback_data="user_edit_recharge"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="user_cancel_recharge")
        )

        logging.info(f"[RECHARGE][{user_id}] تأكيد معلومات الشحن: مبلغ {amount}")
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

        if call.data == "user_confirm_recharge":
            data = recharge_requests.get(user_id)
            if not data:
                logging.warning(f"[RECHARGE][{user_id}] تأكيد طلب شحن بدون بيانات")
                bot.answer_callback_query(call.id, "لا يوجد طلب قيد المعالجة.")
                return

            name = call.from_user.full_name if hasattr(call.from_user, "full_name") else call.from_user.first_name
            register_user_if_not_exist(user_id, name)

            caption = (
                f"💳 طلب شحن محفظة جديد:\n"
                f"👤 المستخدم: {call.from_user.first_name} (@{call.from_user.username or 'بدون معرف'})\n"
                f"🆔 ID: {user_id}\n"
                f"💵 المبلغ: {data['amount']:,} ل.س\n"
                f"💳 الطريقة: {data['method']}\n"
                f"🔢 رقم الإشعار: {data['ref']}"
            )

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ قبول الشحن",  callback_data=f"confirm_add_{user_id}_{data['amount']}"),
                types.InlineKeyboardButton("❌ رفض",        callback_data=f"reject_add_{user_id}")
            )
 
            logging.info(f"[RECHARGE][{user_id}] إرسال طلب الشحن للإدارة")
            add_pending_request(
                user_id=user_id,
                username=call.from_user.username,
                request_text=caption,
                payload={
                    "type": "recharge",
                    "amount": data['amount'],
                    "method": data['method'],
                    "ref": data['ref'],
                    "photo": data["photo"],
                }
            )


            bot.send_photo(
                ADMIN_MAIN_ID,
                photo=data["photo"],
                caption=caption,
                reply_markup=markup
            )
            bot.send_message(
                user_id,
                "📨 تم إرسال طلبك إلى الإدارة، الرجاء الانتظار.",
                reply_markup=keyboards.recharge_menu()
            )
            recharge_pending.add(user_id)
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None
            )

        elif call.data == "user_edit_recharge":
            if user_id in recharge_requests:
                recharge_requests[user_id].pop("amount", None)
                recharge_requests[user_id].pop("ref", None)
                logging.info(f"[RECHARGE][{user_id}] تعديل طلب الشحن")
                bot.send_message(
                    user_id,
                    "🔄 يمكنك الآن إدخال رقم الإشعار / رمز العملية من جديد:",
                    reply_markup=keyboards.recharge_menu()
                )
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

        elif call.data == "user_cancel_recharge":
            clear_pending_request(user_id)
            logging.info(f"[RECHARGE][{user_id}] ألغى طلب الشحن نهائياً")
            bot.send_message(
                user_id,
                "❌ تم إلغاء الطلب، يمكنك البدء من جديد.",
                reply_markup=keyboards.recharge_menu()
            )
            # تصحيح history قبل استدعاء start_recharge_menu
            if not isinstance(history.get(user_id), list):
                history[user_id] = []

            from types import SimpleNamespace
            fake_msg = SimpleNamespace()
            fake_msg.from_user = SimpleNamespace()
            fake_msg.from_user.id = user_id
            fake_msg.chat = SimpleNamespace()
            fake_msg.chat.id = user_id

            start_recharge_menu(bot, fake_msg, history)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
