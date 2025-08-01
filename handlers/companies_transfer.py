from telebot import types
from services.wallet_service import add_purchase, get_balance, has_sufficient_balance, deduct_balance
from database.db import get_table
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from handlers import keyboards
from services.queue_service import add_pending_request
from services.queue_service import process_queue
import logging

user_states = {}

COMMISSION_PER_50000 = 1500

def calculate_commission(amount):
    blocks = amount // 50000
    remainder = amount % 50000
    commission = blocks * COMMISSION_PER_50000
    if remainder > 0:
        commission += int(COMMISSION_PER_50000 * (remainder / 50000))
    return commission

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def companies_transfer_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("شركة الهرم", callback_data="company_alharam"),
        types.InlineKeyboardButton("شركة الفؤاد", callback_data="company_alfouad"),
        types.InlineKeyboardButton("شركة شخاشير", callback_data="company_shakhashir"),
        types.InlineKeyboardButton("⬅️ رجوع", callback_data="back"),
        types.InlineKeyboardButton("🔄 ابدأ من جديد", callback_data="restart")
    )
    return kb

def get_balance(user_id):
    from services.wallet_service import get_balance as get_bal
    return get_bal(user_id)

def deduct_balance(user_id, amount):
    from services.wallet_service import deduct_balance as deduct_bal
    deduct_bal(user_id, amount)

def register_companies_transfer(bot, history):

    @bot.message_handler(func=lambda msg: msg.text == "حوالة مالية عبر شركات")
    def open_companies_menu(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id)
        user_states[user_id] = {"step": None}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("companies_menu")

        logging.info(f"[COMPANY][{user_id}] فتح قائمة تحويل الشركات")
        bot.send_message(msg.chat.id, "💸 اختر الشركة التي تريد التحويل عبرها:", reply_markup=companies_transfer_menu())

    @bot.callback_query_handler(func=lambda call: call.data in [
        "company_alharam", "company_alfouad", "company_shakhashir"
    ])
    def select_company(call):
        user_id = call.from_user.id

        # تحقق طلب معلق مسبق
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            bot.answer_callback_query(call.id, "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.", show_alert=True)
            return

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
        text = (
            "⚠️ تنويه:\n"
            f"العمولة عن كل 50000 ل.س هي {COMMISSION_PER_50000} ل.س.\n"
            "هل ترغب بمتابعة تنفيذ حوالة عبر الشركة المختارة؟"
        )
        kb = make_inline_buttons(
            ("✅ موافق", "company_commission_confirm"),
            ("❌ إلغاء", "company_commission_cancel")
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "company_commission_cancel")
    def company_commission_cancel(call):
        user_id = call.from_user.id
        logging.info(f"[COMPANY][{user_id}] ألغى العملية من شاشة العمولة")
        bot.edit_message_text("❌ تم إلغاء العملية.", call.message.chat.id, call.message.message_id)
        user_states.pop(user_id, None)

    @bot.callback_query_handler(func=lambda call: call.data == "company_commission_confirm")
    def company_commission_confirm(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_beneficiary_name"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel")
        )
        logging.info(f"[COMPANY][{user_id}] وافق على العمولة، ينتظر اسم المستفيد")
        bot.edit_message_text(
            "👤 أرسل اسم المستفيد (الاسم الكنية ابن الأب):",
            call.message.chat.id, call.message.message_id,
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_beneficiary_name")
    def get_beneficiary_name(msg):
        user_id = msg.from_user.id
        user_states[user_id]["beneficiary_name"] = msg.text.strip()
        user_states[user_id]["step"] = "confirm_beneficiary_name"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_beneficiary_name"),
            ("✔️ تأكيد", "beneficiary_name_confirm")
        )
        logging.info(f"[COMPANY][{user_id}] أدخل اسم المستفيد: {msg.text.strip()}")
        bot.send_message(
            msg.chat.id,
            f"👤 الاسم المدخل: {msg.text}\n\nهل تريد المتابعة؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_beneficiary_name")
    def edit_beneficiary_name(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_beneficiary_name"
        bot.send_message(call.message.chat.id, "👤 أعد إرسال اسم المستفيد (الاسم الكنية ابن الأب):")

    @bot.callback_query_handler(func=lambda call: call.data == "beneficiary_name_confirm")
    def beneficiary_name_confirm(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_beneficiary_number"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel")
        )
        logging.info(f"[COMPANY][{user_id}] تأكيد اسم المستفيد: {user_states[user_id].get('beneficiary_name')}")
        bot.edit_message_text("📱 أرسل رقم المستفيد (يجب أن يبدأ بـ 09):", call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_beneficiary_number")
    def get_beneficiary_number(msg):
        user_id = msg.from_user.id
        number = msg.text.strip()
        if not (number.startswith("09") and number.isdigit() and len(number) == 10):
            logging.warning(f"[COMPANY][{user_id}] رقم مستفيد غير صالح: {number}")
            bot.send_message(msg.chat.id, "⚠️ يجب أن يبدأ الرقم بـ 09 ويتكون من 10 أرقام.")
            return
        user_states[user_id]["beneficiary_number"] = number
        user_states[user_id]["step"] = "confirm_beneficiary_number"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_beneficiary_number"),
            ("✔️ تأكيد", "beneficiary_number_confirm")
        )
        logging.info(f"[COMPANY][{user_id}] أدخل رقم المستفيد: {number}")
        bot.send_message(
            msg.chat.id,
            f"📱 الرقم المدخل: {number}\n\nهل تريد المتابعة؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_beneficiary_number")
    def edit_beneficiary_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_beneficiary_number"
        bot.send_message(call.message.chat.id, "📱 أعد إرسال رقم المستفيد (يجب أن يبدأ بـ 09):")

    @bot.callback_query_handler(func=lambda call: call.data == "beneficiary_number_confirm")
    def beneficiary_number_confirm(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_transfer_amount"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel")
        )
        logging.info(f"[COMPANY][{user_id}] تأكيد رقم المستفيد: {user_states[user_id].get('beneficiary_number')}")
        bot.edit_message_text("💵 أرسل المبلغ المراد تحويله (مثال: 12345):", call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_transfer_amount")
    def get_transfer_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            logging.warning(f"[COMPANY][{user_id}] محاولة إدخال مبلغ غير صالح: {msg.text}")
            bot.send_message(msg.chat.id, "⚠️ الرجاء إدخال مبلغ صحيح بالأرقام فقط.")
            return

        commission = calculate_commission(amount)
        total = amount + commission
        user_states[user_id]["amount"] = amount
        user_states[user_id]["commission"] = commission
        user_states[user_id]["total"] = total

        # تحقق طلب معلق مسبق قبل تأكيد المبلغ
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            bot.send_message(msg.chat.id, "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.")
            return

        user_states[user_id]["step"] = "confirming_transfer"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_transfer_amount"),
            ("✔️ تأكيد", "company_transfer_confirm")
        )
        summary = (
            f"📤 تأكيد العملية:\n"
            f"👤 المستفيد: {user_states[user_id]['beneficiary_name']}\n"
            f"📱 رقم المستفيد: {user_states[user_id]['beneficiary_number']}\n"
            f"💸 المبلغ: {amount:,} ل.س\n"
            f"🧾 العمولة: {commission:,} ل.س\n"
            f"✅ الإجمالي: {total:,} ل.س\n"
            f"🏢 الشركة: {user_states[user_id]['company']}\n"
        )
        logging.info(f"[COMPANY][{user_id}] مبلغ التحويل: {amount}, عمولة: {commission}, إجمالي: {total}")
        bot.send_message(msg.chat.id, summary, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_transfer_amount")
    def edit_transfer_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_transfer_amount"
        bot.send_message(call.message.chat.id, "💵 أعد إرسال المبلغ (مثال: 12345):")

    @bot.callback_query_handler(func=lambda call: call.data == "company_transfer_confirm")
    def company_transfer_confirm(call):
        user_id = call.from_user.id
        data = user_states.get(user_id, {})
        amount = data.get('amount')
        commission = data.get('commission')
        total = data.get('total')
        balance = get_balance(user_id)

        if balance < total:
            shortage = total - balance
            logging.warning(f"[COMPANY][{user_id}] محاولة تحويل بمبلغ يفوق الرصيد (الرصيد: {balance}, المطلوب: {total})")
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet"),
                ("⬅️ رجوع", "company_commission_cancel")
            )
            bot.edit_message_text(
                f"❌ لا يوجد رصيد كافٍ في محفظتك.\n"
                f"الإجمالي المطلوب: {total:,} ل.س\n"
                f"رصيدك الحالي: {balance:,} ل.س\n"
                f"المبلغ الناقص: {shortage:,} ل.س\n"
                "يرجى شحن المحفظة أو العودة.",
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
            return
        deduct_balance(user_id, total)
        user_states[user_id]["step"] = "waiting_admin"
        kb_admin = make_inline_buttons(
            ("✅ تأكيد الحوالة", f"admin_company_accept_{user_id}_{total}"),
            ("❌ رفض الحوالة", f"admin_company_reject_{user_id}")
        )
        msg = (
            f"📤 طلب حوالة مالية عبر شركات:\n"
            f"👤 المستخدم: {user_id}\n"
            f"👤 المستفيد: {data.get('beneficiary_name')}\n"
            f"📱 رقم المستفيد: {data.get('beneficiary_number')}\n"
            f"💰 المبلغ: {amount:,} ل.س\n"
            f"🏢 الشركة: {data.get('company')}\n"
            f"🧾 العمولة: {commission:,} ل.س\n"
            f"✅ الإجمالي: {total:,} ل.س\n\n"
            f"يمكنك الرد برسالة أو صورة ليصل للعميل."
        )
        logging.info(f"[COMPANY][{user_id}] طلب حوالة جديد: {data}")
        bot.edit_message_text(
            "✅ تم إرسال الطلب، بانتظار موافقة الإدارة.",
            call.message.chat.id,
            call.message.message_id
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
            }
        )
        bot.send_message(
            user_id,
            "📝 تم إرسال طلبك إلى الإدارة (الطابور).\n"
            "سيتم تنفيذ العملية بعد الموافقة خلال دقائق.\n"
            "يرجى انتظار إشعار التنفيذ أو التواصل مع الإدارة عند الحاجة."
        )
        process_queue(bot)
       

    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet")
    def show_recharge_methods(call):
        bot.send_message(call.message.chat.id, "💳 اختر طريقة شحن المحفظة:", reply_markup=keyboards.recharge_menu())

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_company_accept_"))
    def admin_accept_company_transfer(call):
        try:
            parts = call.data.split("_")
            user_id = int(parts[-2])
            total = int(parts[-1])
            # جلب بيانات الطلب من الطابور
            from database.db import get_table
            res = get_table("pending_requests").select("payload").eq("user_id", user_id).execute()
            if not res.data:
                bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
                return
            payload = res.data[0].get("payload", {})
            reserved = payload.get("reserved", total)
            company = payload.get("company")
            beneficiary_name = payload.get("beneficiary_name")
            beneficiary_number = payload.get("beneficiary_number")
            amount = payload.get("amount")

            if not has_sufficient_balance(user_id, reserved):
                logging.warning(f"[COMPANY][ADMIN][{user_id}] فشل الحوالة، لا يوجد رصيد كافٍ")
                bot.send_message(user_id, "❌ فشل الحوالة: لا يوجد رصيد كافٍ في محفظتك.")
                bot.answer_callback_query(call.id, "❌ لا يوجد رصيد كافٍ لدى العميل.")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                return

            # خصم الرصيد فعليًا الآن فقط!
            from services.wallet_service import deduct_balance, add_purchase
            deduct_balance(user_id, reserved)
            add_purchase(
                user_id,
                reserved,
                f"حوالة مالية عبر {company}",
                reserved,
                beneficiary_number,
            )

            logging.info(f"[COMPANY][ADMIN][{user_id}] تم الخصم وقبول الحوالة، الإجمالي: {reserved}")
            bot.send_message(
                user_id,
                f"✅ تم تنفيذ الحوالة عبر {company} للمستفيد {beneficiary_name} بمبلغ {amount:,} ل.س بنجاح."
            )
            bot.answer_callback_query(call.id, "✅ تم قبول الطلب")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

            def forward_admin_message(m):
                if m.content_type == "photo":
                    file_id = m.photo[-1].file_id
                    bot.send_photo(user_id, file_id, caption=m.caption or "تمت العملية بنجاح.")
                else:
                    bot.send_message(user_id, m.text or "تمت العملية بنجاح.")
            bot.send_message(call.message.chat.id, "📝 أرسل رسالة أو صورة للعميل مع صورة الحوالة أو تأكيد العملية.")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, forward_admin_message)
            # حذف الطلب من الطابور
            from services.queue_service import delete_pending_request
            delete_pending_request(payload.get("id") or res.data[0].get("id"))
            user_states.pop(user_id, None)
        except Exception as e:
            logging.error(f"[COMPANY][ADMIN][{user_id}] خطأ أثناء تأكيد الحوالة: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")


    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_company_reject_"))
    def admin_reject_company_transfer(call):
        try:
            user_id = int(call.data.split("_")[-1])
            logging.info(f"[COMPANY][ADMIN][{user_id}] تم رفض الحوالة من الإدارة")
            def handle_reject(m):
                txt = m.text if m.content_type == "text" else "❌ تم رفض الطلب."
                if m.content_type == "photo":
                    bot.send_photo(user_id, m.photo[-1].file_id, caption=(m.caption or txt))
                else:
                    bot.send_message(user_id, f"❌ تم رفض الطلب من الإدارة.\n📝 السبب: {txt}")
                bot.answer_callback_query(call.id, "❌ تم رفض الطلب")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                user_states.pop(user_id, None)
            bot.send_message(call.message.chat.id, "📝 اكتب سبب الرفض أو أرسل صورة:")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_reject)
        except Exception as e:
            logging.error(f"[COMPANY][ADMIN][{user_id}] خطأ في رفض الحوالة: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")
