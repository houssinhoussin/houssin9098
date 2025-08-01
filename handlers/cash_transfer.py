from services.wallet_service import add_purchase, get_balance, has_sufficient_balance, deduct_balance
from database.db import get_table
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from handlers import keyboards
from services.queue_service import add_pending_request
from services.queue_service import process_queue
import math  # لإدارة صفحات الكيبورد
import logging

user_states = {}

CASH_TYPES = [
    "تحويل إلى سيرياتيل كاش",
    "تحويل إلى أم تي إن كاش",
    "تحويل إلى شام كاش",
]

CASH_PAGE_SIZE = 3

def build_cash_menu(page: int = 0):
    total = len(CASH_TYPES)
    pages = max(1, math.ceil(total / CASH_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    start = page * CASH_PAGE_SIZE
    end = start + CASH_PAGE_SIZE
    for idx, label in enumerate(CASH_TYPES[start:end], start=start):
        kb.add(types.InlineKeyboardButton(label, callback_data=f"cash_sel_{idx}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"cash_page_{page-1}"))
    nav.append(types.InlineKeyboardButton(f"{page+1}/{pages}", callback_data="cash_noop"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"cash_page_{page+1}"))
    kb.row(*nav)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="commission_cancel"))
    return kb

COMMISSION_PER_50000 = 3500

def calculate_commission(amount):
    blocks = amount // 50000
    remainder = amount % 50000
    commission = blocks * COMMISSION_PER_50000
    if remainder > 0:
        commission += int(COMMISSION_PER_50000 * (remainder / 50000))
    return commission

def start_cash_transfer(bot, message, history=None):
    user_id = message.from_user.id
    register_user_if_not_exist(user_id)
    if history is not None:
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
    logging.info(f"[CASH][{user_id}] فتح قائمة تحويل كاش")
    bot.send_message(
        message.chat.id,
        "📤 اختر نوع التحويل من محفظتك:",
        reply_markup=build_cash_menu(0)
    )

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def get_balance(user_id):
    from services.wallet_service import get_balance as get_bal
    return get_bal(user_id)

def deduct_balance(user_id, amount):
    from services.wallet_service import deduct_balance as deduct_bal
    deduct_bal(user_id, amount)

def register(bot, history):

    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_page_"))
    def _paginate_cash_menu(call):
        page = int(call.data.split("_")[-1])
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_cash_menu(page)
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_sel_"))
    def _cash_type_selected(call):
        idx = int(call.data.split("_")[-1])
        if idx < 0 or idx >= len(CASH_TYPES):
            logging.warning(f"[CASH][{call.from_user.id}] اختيار نوع كاش غير صالح: {idx}")
            bot.answer_callback_query(call.id, "❌ خيار غير صالح.")
            return
        cash_type = CASH_TYPES[idx]
        user_id = call.from_user.id

        # تحقق طلب معلق مسبق
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            bot.answer_callback_query(call.id, "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.", show_alert=True)
            return

        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type}")
        text = (
            "⚠️ تنويه:\n"
            f"العمولة لكل 50000 ل.س هي {COMMISSION_PER_50000} ل.س.\n"
            "هل تريد المتابعة وكتابة الرقم المراد التحويل له؟"
        )
        kb = make_inline_buttons(
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(call.id)

    @bot.message_handler(func=lambda msg: msg.text == "💵 تحويل الى رصيد كاش")
    def open_cash_menu(msg):
        start_cash_transfer(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text in CASH_TYPES)
    def handle_cash_type(msg):
        user_id = msg.from_user.id

        # تحقق طلب معلق مسبق
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            bot.send_message(msg.chat.id, "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.")
            return

        cash_type = msg.text
        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type} (من رسالة)")
        text = (
            "⚠️ تنويه:\n"
            f"العمولة لكل 50000 ل.س هي {COMMISSION_PER_50000} ل.س.\n"
            "هل تريد المتابعة وكتابة الرقم المراد التحويل له؟"
        )
        kb = make_inline_buttons(
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        bot.send_message(msg.chat.id, text, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "commission_cancel")
    def commission_cancel(call):
        user_id = call.from_user.id
        logging.info(f"[CASH][{user_id}] ألغى عملية التحويل")
        bot.edit_message_text("❌ تم إلغاء العملية.", call.message.chat.id, call.message.message_id)
        user_states.pop(user_id, None)

    @bot.callback_query_handler(func=lambda call: call.data == "commission_confirm")
    def commission_confirmed(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_number"
        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel")
        )
        bot.edit_message_text("📲 أكتب الرقم المراد التحويل له:", call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_number")
    def get_target_number(msg):
        user_id = msg.from_user.id
        user_states[user_id]["number"] = msg.text
        user_states[user_id]["step"] = "confirm_number"
        logging.info(f"[CASH][{user_id}] أدخل رقم التحويل: {msg.text}")
        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel"),
            ("✏️ تعديل", "edit_number"),
            ("✔️ تأكيد", "number_confirm")
        )
        bot.send_message(
            msg.chat.id,
            f"الرقم المدخل: {msg.text}\n\nهل تريد المتابعة؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_number")
    def edit_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_number"
        bot.send_message(call.message.chat.id, "📲 أعد كتابة الرقم المراد التحويل له:")

    @bot.callback_query_handler(func=lambda call: call.data == "number_confirm")
    def number_confirm(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel")
        )
        bot.edit_message_text("💰 اكتب المبلغ الذي تريد تحويله:", call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_amount")
    def get_amount_and_confirm(msg):
        user_id = msg.from_user.id
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            logging.warning(f"[CASH][{user_id}] محاولة إدخال مبلغ غير صالح: {msg.text}")
            bot.send_message(msg.chat.id, "⚠️ الرجاء إدخال مبلغ صحيح بالأرقام.")
            return

        state = user_states[user_id]
        commission = calculate_commission(amount)
        total = amount + commission
        state["amount"] = amount
        state["commission"] = commission
        state["total"] = total

        # تحقق طلب معلق مسبق عند تأكيد المبلغ
        existing = get_table("pending_requests").select("id").eq("user_id", user_id).execute()
        if existing.data:
            bot.send_message(msg.chat.id, "❌ لديك طلب قيد الانتظار، الرجاء الانتظار حتى الانتهاء.")
            return

        state["step"] = "confirming"
        logging.info(f"[CASH][{user_id}] أدخل مبلغ التحويل: {amount}, عمولة: {commission}, الإجمالي: {total}")

        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel"),
            ("✏️ تعديل", "edit_amount"),
            ("✔️ تأكيد", "cash_confirm")
        )
        summary = (
            f"📤 تأكيد العملية:\n"
            f"📲 الرقم: {state['number']}\n"
            f"💸 المبلغ: {amount:,} ل.س\n"
            f"🧾 العمولة: {commission:,} ل.س\n"
            f"✅ الإجمالي: {total:,} ل.س\n"
            f"💼 الطريقة: {state['cash_type']}"
        )
        bot.send_message(msg.chat.id, summary, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_amount")
    def edit_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        bot.send_message(call.message.chat.id, "💰 أعد كتابة المبلغ:")

    @bot.callback_query_handler(func=lambda call: call.data == "cash_confirm")
    def confirm_transfer(call):
        user_id = call.from_user.id
        data = user_states.get(user_id, {})
        amount = data.get('amount')
        commission = data.get('commission')
        total = data.get('total')
        balance = get_balance(user_id)

        if balance < total:
            logging.warning(f"[CASH][{user_id}] محاولة تحويل بمبلغ يفوق الرصيد. الرصيد: {balance}, المطلوب: {total}")
            shortage = total - balance
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet"),
                ("⬅️ رجوع", "commission_cancel")
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
            ("✅ تأكيد التحويل", f"admin_cash_accept_{user_id}_{total}"),
            ("❌ رفض التحويل", f"admin_cash_reject_{user_id}")
        )
        message = (
            f"📤 طلب تحويل كاش جديد:\n"
            f"👤 المستخدم: {user_id}\n"
            f"📲 الرقم: {data.get('number')}\n"
            f"💰 المبلغ: {amount:,} ل.س\n"
            f"💼 الطريقة: {data.get('cash_type')}\n"
            f"🧾 العمولة: {commission:,} ل.س\n"
            f"✅ الإجمالي: {total:,} ل.س\n\n"
            f"يمكنك الرد برسالة أو صورة ليصل للعميل."
        )
        logging.info(f"[CASH][{user_id}] طلب تحويل جديد: {data}")
        bot.edit_message_text("✅ تم إرسال الطلب، بانتظار موافقة الإدارة.", call.message.chat.id, call.message.message_id)

        # إضافة الطلب مع الحجز للطابور
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=(
                f"تحويل كاش:\n"
                f"📲 الرقم: {data.get('number')}\n"
                f"💰 المبلغ: {amount:,} ل.س\n"
                f"💼 الطريقة: {data.get('cash_type')}\n"
                f"🧾 العمولة: {commission:,} ل.س\n"
                f"✅ الإجمالي: {total:,} ل.س",
            ),
            payload={
                "type": "cash_transfer",
                "number": data.get('number'),
                "cash_type": data.get('cash_type'),
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

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_cash_accept_"))
    def admin_accept_cash_transfer(call):
        try:
            parts = call.data.split("_")
            user_id = int(parts[-2])
            total = int(parts[-1])
            # من المفترض أن تستخرج بيانات الطلب من جدول الطابور!
            from database.db import get_table
            res = get_table("pending_requests").select("payload").eq("user_id", user_id).execute()
            if not res.data:
                bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
                return
            payload = res.data[0].get("payload", {})
            # تأكد من الحجز (reserved) من الطابور وليس من الحالة المؤقتة!
            reserved = payload.get("reserved", total)
            number = payload.get("number")
            cash_type = payload.get("cash_type")

            if not has_sufficient_balance(user_id, reserved):
                logging.warning(f"[CASH][ADMIN][{user_id}] فشل التحويل، لا يوجد رصيد كافٍ")
                bot.send_message(user_id, f"❌ فشل تحويل الكاش: لا يوجد رصيد كافٍ في محفظتك.")
                bot.answer_callback_query(call.id, "❌ لا يوجد رصيد كافٍ لدى العميل.")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                return

            # إعادة الحجز أولًا (لو كان خُصم بالحجز الافتراضي في مكان آخر - لتفادي أي خصم مزدوج)
            # ثم نخصم فعليًا
            from services.wallet_service import deduct_balance, add_purchase
            deduct_balance(user_id, reserved)
            add_purchase(user_id, reserved, f"تحويل كاش {cash_type}", reserved, number)

            logging.info(f"[CASH][ADMIN][{user_id}] تم الخصم وقبول التحويل، الإجمالي: {reserved}")
            bot.send_message(
                user_id,
                f"✅ تم شراء {cash_type} للرقم {number} بمبلغ {reserved:,} ل.س بنجاح."
            )
            bot.answer_callback_query(call.id, "✅ تم قبول الطلب")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

            def forward_admin_message(m):
                if m.content_type == "photo":
                    file_id = m.photo[-1].file_id
                    bot.send_photo(user_id, file_id, caption=m.caption or "تمت العملية بنجاح.")
                else:
                    bot.send_message(user_id, m.text or "تمت العملية بنجاح.")
            bot.send_message(call.message.chat.id, "📝 أرسل رسالة أو صورة للعميل مع صورة التحويل أو تأكيد العملية.")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, forward_admin_message)
            # احذف الطلب من الطابور بعد التنفيذ
            from services.queue_service import delete_pending_request
            delete_pending_request(payload.get("id") or res.data[0].get("id"))
            # نظّف حالة المستخدم المؤقتة إن وجدت
            user_states.pop(user_id, None)
        except Exception as e:
            logging.error(f"[CASH][ADMIN][{user_id}] خطأ أثناء تأكيد التحويل: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")


    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_cash_reject_"))
    def admin_reject_cash_transfer(call):
        try:
            user_id = int(call.data.split("_")[-1])
            logging.info(f"[CASH][ADMIN][{user_id}] تم رفض طلب التحويل")
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
            logging.error(f"[CASH][ADMIN] خطأ في رفض التحويل: {e}", exc_info=True)
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")
