# -*- coding: utf-8 -*-
# handlers/university_fees.py — دفع رسوم جامعية مع HOLD ذري + رسائل موحّدة + /cancel
from handlers.start import _reset_user_flows
_reset_user_flows(m.from_user.id)

from telebot import types
from services.wallet_service import (
    add_purchase,
    get_balance,
    has_sufficient_balance,   # احتياطي لمسارات قديمة
    deduct_balance,           # احتياطي لمسارات قديمة
    create_hold,              # ✅ حجز
    capture_hold,             # ✅ تصفية الحجز
    release_hold,             # ✅ فكّ الحجز
    get_available_balance,    # ✅ المتاح = balance - held (مهم)
)
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
from handlers import keyboards
from services.queue_service import add_pending_request, process_queue, delete_pending_request
from database.db import get_table
import logging

# حارس تأكيد موحّد: يحذف الكيبورد فقط + يعمل Debounce
try:
    from services.ui_guards import confirm_guard
except Exception:
    from ui_guards import confirm_guard

# (اختياري) Validator مركزي للأرقام لو متاح
try:
    from services.validators import parse_amount
except Exception:
    try:
        from validators import parse_amount
    except Exception:
        parse_amount = None  # هنرجع لـ int() لو مش موجود

user_uni_state = {}

COMMISSION_PER_50000 = 3500
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."

def _card(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def _name(bot, user_id: int) -> str:
    try:
        ch = bot.get_chat(user_id)
        name = (getattr(ch, "first_name", None) or getattr(ch, "full_name", "") or "").strip()
        return name or "صاحبنا"
    except Exception:
        return "صاحبنا"

def _fmt(n) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def calculate_uni_commission(amount: int) -> int:
    # ✅ حسْب بالعدد الصحيح فقط (بدون float) — 3500 لكل 50,000 + جزء نسبي
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

def university_fee_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("⬅️ رجوع", callback_data="back"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="uni_cancel")
    )
    return kb

def register_university_fees(bot, history):

    # /cancel — إلغاء فوري من أي خطوة
    @bot.message_handler(commands=['cancel'])
    def cancel_any(msg):
        uid = msg.from_user.id
        name = _name(bot, uid)
        user_uni_state.pop(uid, None)
        bot.send_message(
            msg.chat.id,
            _card("✅ تم الإلغاء", [f"يا {name}، رجعناك لبدء العملية من جديد."]),
        )

    @bot.message_handler(func=lambda msg: msg.text == "🎓 دفع رسوم جامعية")
    def open_uni_menu(msg):
        user_id = msg.from_user.id
        name = _name(bot, user_id)
        register_user_if_not_exist(user_id)
        user_uni_state[user_id] = {"step": "university_name"}
        history.setdefault(user_id, []).append("university_fees_menu")
        kb = university_fee_menu()
        bot.send_message(
            msg.chat.id,
            _card("🏫 بيانات الجامعة", [f"يا {name}، اكتب اسم الجامعة وفي أنهي محافظة.", "", CANCEL_HINT]),
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "university_name")
    def enter_university(msg):
        user_id = msg.from_user.id
        name = _name(bot, user_id)
        user_uni_state[user_id]["university"] = (msg.text or "").strip()
        user_uni_state[user_id]["step"] = "national_id"
        kb = university_fee_menu()
        bot.send_message(
            msg.chat.id,
            _card("🆔 الرقم الوطني", [f"يا {name}، ابعت الرقم الوطني كامل.", "", CANCEL_HINT]),
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "national_id")
    def enter_national_id(msg):
        user_id = msg.from_user.id
        name = _name(bot, user_id)
        user_uni_state[user_id]["national_id"] = (msg.text or "").strip()
        user_uni_state[user_id]["step"] = "university_id"
        kb = university_fee_menu()
        bot.send_message(
            msg.chat.id,
            _card("🎓 الرقم الجامعي", [f"يا {name}، ابعت الرقم الجامعي.", "", CANCEL_HINT]),
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "university_id")
    def enter_university_id(msg):
        user_id = msg.from_user.id
        name = _name(bot, user_id)
        user_uni_state[user_id]["university_id"] = (msg.text or "").strip()
        user_uni_state[user_id]["step"] = "amount"
        kb = university_fee_menu()
        bot.send_message(
            msg.chat.id,
            _card("💰 مبلغ الرسوم", [f"يا {name}، ابعت المبلغ المطلوب دفعه بالأرقام فقط.", "", CANCEL_HINT]),
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_uni_state.get(msg.from_user.id, {}).get("step") == "amount")
    def enter_amount(msg):
        user_id = msg.from_user.id
        name = _name(bot, user_id)

        txt = (msg.text or "").strip()
        try:
            if parse_amount:
                # ✅ الوسيط الصحيح هو min_value
                amount = parse_amount(txt, min_value=1)
            else:
                amount = int(txt.replace(",", ""))
                if amount <= 0:
                    raise ValueError
        except Exception:
            return bot.send_message(
                msg.chat.id,
                _card("⚠️ مبلغ غير صالح", [f"يا {name}، اكتب رقم صحيح من غير فواصل أو رموز.", "", CANCEL_HINT])
            )

        user_uni_state[user_id]["amount"] = int(amount)

        commission = calculate_uni_commission(amount)
        total = amount + commission

        user_uni_state[user_id]["commission"] = commission
        user_uni_state[user_id]["total"] = total
        user_uni_state[user_id]["step"] = "confirm_details"

        text = _card(
            "🧾 تأكيد البيانات",
            [
                f"🏫 الجامعة: {user_uni_state[user_id]['university']}",
                f"🆔 الرقم الوطني: {user_uni_state[user_id]['national_id']}",
                f"🎓 الرقم الجامعي: {user_uni_state[user_id]['university_id']}",
                f"💰 المبلغ: {_fmt(amount)}",
                f"🧾 العمولة: {_fmt(commission)}",
                f"✅ الإجمالي: {_fmt(total)}",
                "",
                "لو تمام اضغط تأكيد، أو عدّل/الغِ الطلب.",
                CANCEL_HINT,
            ]
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
        name = _name(bot, user_id)
        state = user_uni_state.get(user_id, {})
        current_step = state.get("step")

        if current_step == "national_id":
            state["step"] = "university_name"
            bot.edit_message_text(
                _card("🏫 بيانات الجامعة", [f"يا {name}، اكتب اسم الجامعة وفي أنهي محافظة.", "", CANCEL_HINT]),
                call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu()
            )
        elif current_step == "university_id":
            state["step"] = "national_id"
            bot.edit_message_text(
                _card("🆔 الرقم الوطني", [f"يا {name}، ابعت الرقم الوطني كامل.", "", CANCEL_HINT]),
                call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu()
            )
        elif current_step == "amount":
            state["step"] = "university_id"
            bot.edit_message_text(
                _card("🎓 الرقم الجامعي", [f"يا {name}، ابعت الرقم الجامعي.", "", CANCEL_HINT]),
                call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu()
            )
        elif current_step == "confirm_details":
            state["step"] = "amount"
            bot.edit_message_text(
                _card("💰 مبلغ الرسوم", [f"يا {name}، ابعت المبلغ المطلوب دفعه بالأرقام فقط.", "", CANCEL_HINT]),
                call.message.chat.id, call.message.message_id, reply_markup=university_fee_menu()
            )
        else:
            user_uni_state.pop(user_id, None)
            bot.edit_message_text("❌ رجعناك للقائمة الرئيسية.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_university_fees")
    def edit_university_fees(call):
        user_id = call.from_user.id
        user_uni_state[user_id]["step"] = "amount"
        bot.send_message(call.message.chat.id, _card("✏️ تعديل المبلغ", ["ابعت المبلغ من جديد:", "", CANCEL_HINT]))

    @bot.callback_query_handler(func=lambda call: call.data == "uni_cancel")
    def cancel_uni(call):
        name = _name(bot, call.from_user.id)
        user_uni_state.pop(call.from_user.id, None)
        bot.edit_message_text(_card("🚫 تم الإلغاء", [f"تمام يا {name}، اتلغت العملية.", "", "تقدر تبدأ من جديد في أي وقت."]), call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda call: call.data == "uni_confirm")
    def confirm_uni_order(call):
        # ✅ احذف الكيبورد وامنَع الدبل-كليك
        if confirm_guard(bot, call, "uni_confirm"):
            return

        user_id = call.from_user.id
        name = _name(bot, user_id)
        state = user_uni_state.get(user_id, {})
        total = int(state.get("total") or 0)

        # فحص الرصيد المتاح (balance - held)
        balance_av = get_available_balance(user_id)
        if balance_av is None or balance_av < total:
            shortage = total - (balance_av or 0)
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet_uni"),
                ("⬅️ رجوع", "uni_cancel")
            )
            bot.edit_message_text(
                _card("❌ رصيدك مش مكفّي", [f"الإجمالي المطلوب: {_fmt(total)}", f"متاحك: {_fmt(balance_av or 0)}", f"الناقص: {_fmt(shortage)}", "", "اشحن محفظتك أو ارجع خطوة."]),
                call.message.chat.id, call.message.message_id,
                reply_markup=kb
            )
            return

        # ✅ حجز بدل الخصم الفوري — ذري
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
            try:
                return bot.answer_callback_query(call.id, f"⚠️ يا {name}، حصلت مشكلة أثناء تثبيت العملية. جرّب تاني.", show_alert=True)
            except Exception:
                return

        # رسالة الإدارة (موحّدة + رصيد حالي)
        balance_now = get_balance(user_id)
        admin_msg = (
            f"💰 رصيد المستخدم: {int(balance_now or 0):,} ل.س\n"
            f"🆕 طلب جديد — رسوم جامعية\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🏫 الجامعة: <code>{state.get('university')}</code>\n"
            f"🆔 الوطني: <code>{state.get('national_id')}</code>\n"
            f"🎓 الجامعي: <code>{state.get('university_id')}</code>\n"
            f"💵 المبلغ: {int(state.get('amount') or 0):,} ل.س\n"
            f"🧾 العمولة: {int(state.get('commission') or 0):,} ل.س\n"
            f"✅ الإجمالي (محجوز): {total:,} ل.س\n"
            f"(university_fees) — HOLD: {hold_id}"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "university_fees",
                "university": state.get('university'),
                "national_id": state.get('national_id'),
                "university_id": state.get('university_id'),
                "amount": int(state.get('amount') or 0),
                "commission": int(state.get('commission') or 0),
                "total": total,
                "reserved": total,
                "hold_id": hold_id,   # ✅ مفتاح مهم للأدمن
            }
        )
        user_uni_state[user_id]["step"] = "waiting_admin"

        process_queue(bot)
        bot.edit_message_text(
            _card("✅ تمام! طلبك في السكة 🚀", ["بعتنا الطلب للإدارة.", "التنفيذ عادة خلال 1–4 دقايق وهيوصلك إشعار أول ما يتم.", "", "تقدر تبعت طلبات تانية — الحجز من المتاح بس 😉"]),
            call.message.chat.id, call.message.message_id
        )

    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet_uni")
    def show_recharge_methods_uni(call):
        bot.send_message(call.message.chat.id, "💳 اختار طريقة شحن المحفظة:", reply_markup=keyboards.recharge_menu())

    # =========================
    # هاندلرات قديمة (توافقية)
    # =========================
    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_uni_accept_"))
    def admin_accept_uni_fees(call):
        """توافقياً: القبول الحقيقي من لوحة الأدمن العامة. هنا نصفي الحجز لو اتفعل الزر القديم."""
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
                # ✋ بدون HOLD لا ننصح بخصم يدوي — خلي العملية تعاد بالطريقة الجديدة
                bot.answer_callback_query(call.id, "⚠️ لا يوجد HOLD — ارفض الطلب واطلب إعادة الإرسال.")
                bot.send_message(user_id, "⚠️ حصل تعارض بسيط. رجاءً أعد إرسال الطلب ليتم حجز المبلغ تلقائيًا.")
                return

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

            bot.send_message(call.message.chat.id, "📝 اكتب سبب الرفض أو ابعت صورة (اختياري):")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id, finalize_reject)

        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ حدث خطأ: {e}")
