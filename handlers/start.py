# handlers/university_fees.py
from telebot import types
from services.wallet_service import (
    add_purchase,
    get_balance,
    has_sufficient_balance,
    deduct_balance,   # احتياطي لمسارات قديمة
    create_hold,      # ✅ حجز
    capture_hold,     # ✅ تصفية الحجز
    release_hold,     # ✅ فكّ الحجز
)
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from handlers import keyboards
from services.queue_service import add_pending_request, process_queue, delete_pending_request
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
        types.InlineKeyboardButton("❌ إلغاء", callback_data="uni_cancel")
    )
    return kb

def _user_name(bot, user_id: int) -> str:
    try:
        ch = bot.get_chat(user_id)
        name = (getattr(ch, "first_name", None) or getattr(ch, "full_name", "") or "").strip()
        return name or "صاحبنا"
    except Exception:
        return "صاحبنا"

def register_university_fees(bot, history):

    @bot.message_handler(func=lambda msg: msg.text == "🎓 دفع رسوم جامعية")
    def open_uni_menu(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        register_user_if_not_exist(user_id)
        user_uni_state[user_id] = {"step": "university_name"}
        history.setdefault(user_id, []).append("university_fees_menu")
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, f"🏫 يا {name}، اكتب اسم الجامعة وفي أنهي محافظة:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "university_name")
    def enter_university(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        user_uni_state[user_id]["university"] = msg.text.strip()
        user_uni_state[user_id]["step"] = "national_id"
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, f"🆔 يا {name}، ابعت الرقم الوطني:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "national_id")
    def enter_national_id(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        user_uni_state[user_id]["national_id"] = msg.text.strip()
        user_uni_state[user_id]["step"] = "university_id"
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, f"🎓 يا {name}، ابعت الرقم الجامعي:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "university_id")
    def enter_university_id(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        user_uni_state[user_id]["university_id"] = msg.text.strip()
        user_uni_state[user_id]["step"] = "amount"
        kb = university_fee_menu()
        bot.send_message(msg.chat.id, f"💰 يا {name}، ابعت المبلغ المطلوب دفعه:", reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "amount")
    def enter_amount(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        try:
            amount = int(msg.text.strip())
            if amount <= 0:
                raise ValueError
            user_uni_state[user_id]["amount"] = amount
        except ValueError:
            return bot.send_message(msg.chat.id, f"⚠️ يا {name}، اكتب رقم صحيح للمبلغ.")

        commission = calculate_uni_commission(amount)
        total = amount + commission

        user_uni_state[user_id]["commission"] = commission
        user_uni_state[user_id]["total"] = total
        user_uni_state[user_id]["step"] = "confirm_details"

        text = (
            f"❓ تأكيد دفع الرسوم يا {name}؟\n"
            f"🏫 الجامعة: {user_uni_state[user_id]['university']}\n"
            f"🆔 الرقم الوطني: {user_uni_state[user_id]['national_id']}\n"
            f"🎓 الرقم الجامعي: {user_uni_state[user_id]['university_id']}\n"
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
        
    @bot.callback_query_handler(func=lambda call: call.data == "back")
    def go_back(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        state = user_uni_state.get(user_id, {})
        current_step = state.get("step")

        if current_step == "national_id":
            state["step"] = "university_name"
            bot.edit_message_text(f"🏫 يا {name}، اكتب اسم الجامعة وفي أنهي محافظة:", call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu())
        elif current_step == "university_id":
            state["step"] = "national_id"
            bot.edit_message_text(f"🆔 يا {name}، ابعت الرقم الوطني:", call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu())
        elif current_step == "amount":
            state["step"] = "university_id"
            bot.edit_message_text(f"🎓 يا {name}، ابعت الرقم الجامعي:", call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu())
        elif current_step == "confirm_details":
            state["step"] = "amount"
            bot.edit_message_text(f"💰 يا {name}، ابعت المبلغ المطلوب دفعه:", call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu())
        else:
            user_uni_state.pop(user_id, None)
            bot.edit_message_text("❌ رجعناك للقائمة الرئيسية.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_university_fees")
    def edit_university_fees(call):
        user_id = call.from_user.id
        user_uni_state[user_id]["step"] = "amount"
        bot.send_message(call.message.chat.id, "💰 ابعت المبلغ من جديد:")

    @bot.callback_query_handler(func=lambda call: call.data == "uni_cancel")
    def cancel_uni(call):
        name = _user_name(bot, call.from_user.id)
        user_uni_state.pop(call.from_user.id, None)
        bot.edit_message_text(f"🚫 تمام يا {name}، اتلغت.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda call: call.data == "uni_confirm")
    def confirm_uni_order(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        state = user_uni_state.get(user_id, {})
        total = int(state.get("total") or 0)

        # منع التوازي
        balance = get_available_balance(user_id)
        if balance is None or balance < total:
            shortage = total - (balance or 0)
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet_uni"),
                ("⬅️ رجوع", "uni_cancel")
            )
            bot.edit_message_text(
                f"❌ يا {name}، رصيدك مش مكفي.\n"
                f"الإجمالي المطلوب: {total:,} ل.س\n"
                f"رصيدك الحالي: {balance or 0:,} ل.س\n"
                f"الناقص: {shortage:,} ل.س\n"
                "اشحن المحفظة أو ارجع خطوة.",
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
            return

        # ✅ حجز بدل الخصم الفوري
        hold_id = None
        try:
            reason = f"حجز رسوم جامعية — {state.get('university','')}"
            res = create_hold(user_id, total, reason)
            d = getattr(res, "data", None)
            if isinstance(d, dict):
                hold_id = d.get("id") or d.get("hold_id")
            elif isinstance(d, (list, tuple)) and d:
                hold_id = d[0].get("id") if isinstance(d[0], dict) else d[0]
            elif isinstance(d, (int, str)):
                hold_id = d
        except Exception as e:
            logging.exception(f"[UNI][{user_id}] create_hold failed: {e}")

        if not hold_id:
            return bot.answer_callback_query(call.id, f"⚠️ يا {name}، حصلت مشكلة أثناء تثبيت العملية. جرّب تاني.", show_alert=True)

        # رسالة الإدارة (HTML)
        msg = (
            f"📚 <b>طلب دفع رسوم جامعية</b>\n"
            f"👤 المستخدم: <code>{user_id}</code>\n"
            f"🏫 الجامعة: <b>{state['university']}</b>\n"
            f"🆔 الرقم الوطني: <code>{state['national_id']}</code>\n"
            f"🎓 الرقم الجامعي: <code>{state['university_id']}</code>\n"
            f"💵 المبلغ: <b>{state['amount']:,} ل.س</b>\n"
            f"🧾 العمولة: <b>{state['commission']:,} ل.س</b>\n"
            f"✅ الإجمالي (محجوز): <b>{total:,} ل.س</b>"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=msg,
            payload={
                "type": "university_fees",
                "university": state['university'],
                "national_id": state['national_id'],
                "university_id": state['university_id'],
                "amount": state['amount'],
                "commission": state['commission'],
                "total": state['total'],
                "reserved": total,
                "hold_id": hold_id,   # ✅ مهم
            }
        )
        user_uni_state[user_id]["step"] = "waiting_admin"

        process_queue(bot)
        bot.edit_message_text(
            f"✅ يا {name}، طلبك اتبعت للإدارة. هننفّذ وهنبعتلك إشعار أول ما يخلص.",
            call.message.chat.id, call.message.message_id
        )

    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet_uni")
    def show_recharge_methods_uni(call):
        bot.send_message(call.message.chat.id, "💳 اختر طريقة شحن المحفظة:", reply_markup=keyboards.recharge_menu())

    # =========================
    # هاندلرات قديمة (توافقية)
    # =========================
    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_uni_accept_"))
    def admin_accept_uni_fees(call):
        """توافقياً: في النظام الحالي، القبول بيتم من handlers/admin.py.
        لو اتفعّل الزر ده، هنحاول نلقط الطلب ونصفّي الحجز."""
        try:
            parts = call.data.split("_")
            user_id = int(parts[-2])
            total = int(parts[-1])

            # هات الـpayload من الطابور
            res = get_table("pending_requests").select("id,payload").eq("user_id", user_id).execute()
            if not res.data:
                bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
                return
            row = res.data[0]
            payload = row.get("payload", {}) or {}
            hold_id = payload.get("hold_id")
            university = payload.get("university")

            if hold_id:
                try:
                    r = capture_hold(hold_id)
                    if getattr(r, "error", None) or not bool(getattr(r, "data", True)):
                        return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز.", show_alert=True)
                except Exception as e:
                    logging.exception(f"[UNI][ADMIN][{user_id}] capture_hold failed: {e}")
                    return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز.", show_alert=True)
            else:
                # مسار قديم: خصم فعلي (تجنّب الازدواجية قدر الإمكان)
                if not has_sufficient_balance(user_id, total):
                    bot.answer_callback_query(call.id, "❌ لا يوجد رصيد كافٍ.", show_alert=True)
                    return
                deduct_balance(user_id, total)

            # إعلام المستخدم
            bot.send_message(
                user_id,
                f"✅ تم دفع الرسوم الجامعية ({university}) بنجاح.\n"
                f"المبلغ الإجمالي المدفوع: {total:,} ل.س"
            )
            bot.answer_callback_query(call.id, "✅ تم قبول الطلب")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

            # حذف الطلب من الطابور
            delete_pending_request(row.get("id"))
            user_uni_state.pop(user_id, None)

        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_uni_reject_"))
    def admin_reject_uni_fees(call):
        """توافقياً: لو اتفعّل زر الرفض القديم، نفك الحجز لو موجود."""
        try:
            user_id = int(call.data.split("_")[-1])

            # هات الـpayload
            res = get_table("pending_requests").select("id,payload").eq("user_id", user_id).execute()
            row = res.data[0] if res.data else {}
            payload = row.get("payload", {}) if row else {}
            hold_id = payload.get("hold_id")

            def finalize_reject(m):
                txt = m.text if m.content_type == "text" else "❌ تم رفض الطلب."
                if hold_id:
                    try:
                        release_hold(hold_id)
                    except Exception as e:
                        logging.exception(f"[UNI][ADMIN][{user_id}] release_hold failed: {e}")
                # رسالة للمستخدم
                if m.content_type == "photo":
                    bot.send_photo(user_id, m.photo[-1].file_id, caption=(m.caption or txt))
                else:
                    bot.send_message(user_id, f"❌ تم رفض طلب دفع الرسوم.\n📝 السبب: {txt}")
                bot.answer_callback_query(call.id, "❌ تم رفض الطلب")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                if row:
                    delete_pending_request(row.get("id"))
                user_uni_state.pop(user_id, None)

            bot.send_message(call.message.chat.id, "📝 اكتب سبب الرفض أو ابعت صورة:")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, finalize_reject)

        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")
