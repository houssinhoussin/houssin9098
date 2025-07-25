# handlers/university_fees.py
from telebot import types
from services.wallet_service import add_purchase, get_balance, has_sufficient_balance,deduct_balance
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from handlers import keyboards
from services.queue_service import add_pending_request
from database.db import get_table
import logging

user_uni_state = {}

COMMISSION_PER_50000 = 3500

def calculate_uni_commission(amount):
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

def university_fee_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("⬅️ رجوع", callback_data="back"),
        types.InlineKeyboardButton("🔄 ابدأ من جديد", callback_data="restart")
    )
    return kb

def register_university_fees(bot, history):

    @bot.message_handler(func=lambda msg: msg.text == "🎓 دفع رسوم جامعية")
    def open_uni_menu(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id)
        user_uni_state[user_id] = {"step": "university_name"}
        history.setdefault(user_id, []).append("university_fees_menu")
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, "🏫 اكتب اسم الجامعة أو اسم المحافظة:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "university_name")
    def enter_university(msg):
        user_id = msg.from_user.id
        user_uni_state[user_id]["university"] = msg.text.strip()
        user_uni_state[user_id]["step"] = "national_id"
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, "🆔 أدخل الرقم الوطني:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "national_id")
    def enter_national_id(msg):
        user_id = msg.from_user.id
        user_uni_state[user_id]["national_id"] = msg.text.strip()
        user_uni_state[user_id]["step"] = "university_id"
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, "🎓 أدخل الرقم الجامعي:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "university_id")
    def enter_university_id(msg):
        user_id = msg.from_user.id
        user_uni_state[user_id]["university_id"] = msg.text.strip()
        user_uni_state[user_id]["step"] = "amount"
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, "💰 أدخل المبلغ المطلوب دفعه:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "amount")
    def enter_amount(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text.strip())
            if amount <= 0:
                raise ValueError
            user_uni_state[user_id]["amount"] = amount
        except ValueError:
            return bot.send_message(msg.chat.id, "⚠️ الرجاء إدخال رقم صالح للمبلغ.")

        commission = calculate_uni_commission(amount)
        total = amount + commission

        user_uni_state[user_id]["commission"] = commission
        user_uni_state[user_id]["total"] = total
        user_uni_state[user_id]["step"] = "confirm_details"

        text = (
            f"❓ هل أنت متأكد من دفع رسوم جامعية؟\n"
            f"🏫 الجامعة: {user_uni_state[user_id]['university']}\n"
            f"🆔 رقم وطني: {user_uni_state[user_id]['national_id']}\n"
            f"🎓 رقم جامعي: {user_uni_state[user_id]['university_id']}\n"
            f"💰 المبلغ: {amount:,} ل.س\n"
            f"🧾 العمولة: {commission:,} ل.س\n"
            f"✅ الإجمالي: {total:,} ل.س"
        )

        kb = make_inline_buttons(
            ("✏️ تعديل", "edit_university_fees"),
            ("✔️ تأكيد", "uni_confirm"),
            ("❌ إلغاء", "uni_cancel")
        )
        bot.send_message(msg.chat.id, text, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_university_fees")
    def edit_university_fees(call):
        user_id = call.from_user.id
        user_uni_state[user_id]["step"] = "amount"
        bot.send_message(call.message.chat.id, "💰 أعد إدخال المبلغ المراد دفعه:")

    @bot.callback_query_handler(func=lambda call: call.data == "uni_cancel")
    def cancel_uni(call):
        user_uni_state.pop(call.from_user.id, None)
        bot.edit_message_text("🚫 تم إلغاء العملية.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda call: call.data == "uni_confirm")
    def confirm_uni_order(call):
        user_id = call.from_user.id
        state = user_uni_state.get(user_id, {})
        total = state.get("total")
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            bot.edit_message_text(
                "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.",
                call.message.chat.id,
                call.message.message_id
            )
            return
        balance = get_balance(user_id)
        if balance is None or balance < total:
            shortage = total - (balance or 0)
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet_uni"),
                ("⬅️ رجوع", "uni_cancel")
            )
            bot.edit_message_text(
                f"❌ لا يوجد رصيد كافٍ في محفظتك.\n"
                f"الإجمالي المطلوب: {total:,} ل.س\n"
                f"رصيدك الحالي: {balance or 0:,} ل.س\n"
                f"المبلغ الناقص: {shortage:,} ل.س\n"
                "يرجى شحن المحفظة أو العودة.",
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
            return

        # خصم الرصيد مباشرة من المستخدم
        deduct_balance(user_id, total)

        # إرسال الطلب إلى الأدمن مع أزرار قبول/رفض
        kb_admin = make_inline_buttons(
            ("✅ تأكيد دفع الرسوم", f"admin_uni_accept_{user_id}_{total}"),
            ("❌ رفض الدفع", f"admin_uni_reject_{user_id}")
        )

        msg = (
            f"📚 طلب دفع رسوم جامعية:\n"
            f"👤 المستخدم: {user_id}\n"
            f"🏫 الجامعة: {state['university']}\n"
            f"🆔 الرقم الوطني: {state['national_id']}\n"
            f"🎓 الرقم الجامعي: {state['university_id']}\n"
            f"💵 المبلغ المطلوب: {state['amount']:,} ل.س\n"
            f"🧾 العمولة: {state['commission']:,} ل.س\n"
            f"✅ الإجمالي: {total:,} ل.س"
        )

        bot.edit_message_text("✅ تم إرسال طلبك للإدارة، بانتظار الموافقة.",
                              call.message.chat.id, call.message.message_id)

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=msg
        )
        user_uni_state[user_id]["step"] = "waiting_admin"

    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet_uni")
    def show_recharge_methods_uni(call):
        bot.send_message(call.message.chat.id, "💳 اختر طريقة شحن المحفظة:", reply_markup=keyboards.recharge_menu())

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_uni_accept_"))
    def admin_accept_uni_fees(call):
        try:
            parts = call.data.split("_")
            user_id = int(parts[-2])
            total = int(parts[-1])
            data = user_uni_state.get(user_id, {})
            # هنا العملية منتهية بالفعل لأن الرصيد خُصم عند الطلب، فقط إعلام المستخدم
            bot.send_message(
                user_id,
                f"✅ تم تنفيذ دفع الرسوم الجامعية بنجاح.\n"
                f"🏫 الجامعة: {data.get('university')}\n"
                f"المبلغ الإجمالي المدفوع: {total:,} ل.س"
            )
            bot.answer_callback_query(call.id, "✅ تم قبول الطلب")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            # يمكن للأدمن إرسال رسالة أو صورة إثبات للمستخدم
            def forward_admin_message(m):
                if m.content_type == "photo":
                    file_id = m.photo[-1].file_id
                    bot.send_photo(user_id, file_id, caption=m.caption or "تمت العملية بنجاح.")
                else:
                    bot.send_message(user_id, m.text or "تمت العملية بنجاح.")
            bot.send_message(call.message.chat.id, "📝 أرسل رسالة أو صورة للطالب بعد دفع الرسوم.")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, forward_admin_message)
            user_uni_state.pop(user_id, None)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_uni_reject_"))
    def admin_reject_uni_fees(call):
        try:
            user_id = int(call.data.split("_")[-1])
            def handle_reject(m):
                txt = m.text if m.content_type == "text" else "❌ تم رفض الطلب."
                if m.content_type == "photo":
                    bot.send_photo(user_id, m.photo[-1].file_id, caption=(m.caption or txt))
                else:
                    bot.send_message(user_id, f"❌ تم رفض الطلب من الإدارة.\n📝 السبب: {txt}")
                bot.answer_callback_query(call.id, "❌ تم رفض الطلب")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                user_uni_state.pop(user_id, None)
            bot.send_message(call.message.chat.id, "📝 اكتب سبب الرفض أو أرسل صورة:")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, handle_reject)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")
