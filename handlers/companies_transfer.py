# handlers/companies_transfer.py
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
)
from database.db import get_table
from config import ADMIN_MAIN_ID
from handlers import keyboards
from services.queue_service import add_pending_request, process_queue
import logging

user_states = {}

COMMISSION_PER_50000 = 1500

def _user_name(bot, user_id: int) -> str:
    """اسم مختصر لطيف للرسائل."""
    try:
        ch = bot.get_chat(user_id)
        name = (getattr(ch, "first_name", None) or getattr(ch, "full_name", "") or "").strip()
        return name or "صاحبنا"
    except Exception:
        return "صاحبنا"

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

# حفاظًا على واجهاتك
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
        name = _user_name(bot, user_id)
        register_user_if_not_exist(user_id)
        user_states[user_id] = {"step": None}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("companies_menu")

        logging.info(f"[COMPANY][{user_id}] فتح قائمة تحويل الشركات")
        bot.send_message(msg.chat.id, f"💸 يا {name}، اختار الشركة اللي عايز تحوّل معاها:", reply_markup=companies_transfer_menu())

    @bot.callback_query_handler(func=lambda call: call.data in [
        "company_alharam", "company_alfouad", "company_shakhashir"
    ])
    def select_company(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)

        # طلب قديم لسه في الطابور؟
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
            f"⚠️ تنويه سريع يا {name}:\n"
            f"عمولة كل 50,000 ل.س = {COMMISSION_PER_50000} ل.س.\n"
            "تكمل وتبعت بيانات المستفيد؟"
        )
        kb = make_inline_buttons(
            ("✅ ماشي", "company_commission_confirm"),
            ("❌ إلغاء", "company_commission_cancel")
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "company_commission_cancel")
    def company_commission_cancel(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        logging.info(f"[COMPANY][{user_id}] ألغى العملية من شاشة العمولة")
        bot.edit_message_text(f"✅ تم الإلغاء يا {name}. لو حابب، تقدر تبدأ من جديد في أي وقت.", call.message.chat.id, call.message.message_id)
        user_states.pop(user_id, None)

    @bot.callback_query_handler(func=lambda call: call.data == "company_commission_confirm")
    def company_commission_confirm(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_name"
        kb = make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        logging.info(f"[COMPANY][{user_id}] وافق على العمولة، ينتظر اسم المستفيد")
        bot.edit_message_text(
            f"👤 يا {name}، ابعت اسم المستفيد بالكامل: (الاسم الكنية ابن الأب)",
            call.message.chat.id, call.message.message_id,
            reply_markup=kb
        )

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_beneficiary_name")
    def get_beneficiary_name(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["beneficiary_name"] = msg.text.strip()
        user_states[user_id]["step"] = "confirm_beneficiary_name"
        kb = make_inline_buttons(
            ("❌ إلغاء", "company_commission_cancel"),
            ("✏️ تعديل", "edit_beneficiary_name"),
            ("✔️ تأكيد", "beneficiary_name_confirm")
        )
        logging.info(f"[COMPANY][{user_id}] اسم المستفيد: {msg.text.strip()}")
        bot.send_message(
            msg.chat.id,
            f"👤 تمام يا {name}، الاسم المدخّل:\n{msg.text}\n\nنكمل؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_beneficiary_name")
    def edit_beneficiary_name(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_name"
        bot.send_message(call.message.chat.id, f"👤 تمام يا {name}، ابعت الاسم تاني (الاسم الكنية ابن الأب):")

    @bot.callback_query_handler(func=lambda call: call.data == "beneficiary_name_confirm")
    def beneficiary_name_confirm(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_number"
        kb = make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        logging.info(f"[COMPANY][{user_id}] تأكيد الاسم")
        bot.edit_message_text(f"📱 يا {name}، ابعت رقم المستفيد (لازم يبدأ بـ 09) — 10 أرقام:", call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_beneficiary_number")
    def get_beneficiary_number(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        number = msg.text.strip()
        if not (number.startswith("09") and number.isdigit() and len(number) == 10):
            logging.warning(f"[COMPANY][{user_id}] رقم مستفيد غير صالح: {number}")
            bot.send_message(msg.chat.id, f"⚠️ يا {name}، الرقم لازم يبدأ بـ 09 ويتكوّن من 10 أرقام. جرّب تاني.")
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
            f"📱 تمام يا {name}، الرقم المدخّل:\n{number}\n\nنكمل؟",
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_beneficiary_number")
    def edit_beneficiary_number(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_beneficiary_number"
        bot.send_message(call.message.chat.id, f"📱 يا {name}، ابعت الرقم تاني (لازم يبدأ بـ 09):")

    @bot.callback_query_handler(func=lambda call: call.data == "beneficiary_number_confirm")
    def beneficiary_number_confirm(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_transfer_amount"
        kb = make_inline_buttons(("❌ إلغاء", "company_commission_cancel"))
        logging.info(f"[COMPANY][{user_id}] تأكيد رقم المستفيد")
        bot.edit_message_text(f"💵 يا {name}، ابعت المبلغ اللي عايز تحوّله (مثال: 12345):", call.message.chat.id, call.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_transfer_amount")
    def get_transfer_amount(msg):
        user_id = msg.from_user.id
        name = _user_name(bot, user_id)
        try:
            amount = int(msg.text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            logging.warning(f"[COMPANY][{user_id}] مبلغ غير صالح: {msg.text}")
            bot.send_message(msg.chat.id, f"⚠️ يا {name}، دخل رقم صحيح من غير فواصل أو رموز.")
            return

        commission = calculate_commission(amount)
        total = amount + commission
        user_states[user_id]["amount"] = amount
        user_states[user_id]["commission"] = commission
        user_states[user_id]["total"] = total

        # تأكد مفيش طلب قديم في الطابور
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
        logging.info(f"[COMPANY][{user_id}] amount={amount}, fee={commission}, total={total}")
        bot.send_message(msg.chat.id, f"يا {name}، راجع التفاصيل تحت وبعدين اضغط تأكيد:\n\n{summary}", reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_transfer_amount")
    def edit_transfer_amount(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        user_states[user_id]["step"] = "awaiting_transfer_amount"
        bot.send_message(call.message.chat.id, f"💵 تمام يا {name}، ابعت المبلغ تاني (مثال: 12345):")

    @bot.callback_query_handler(func=lambda call: call.data == "company_transfer_confirm")
    def company_transfer_confirm(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        data = user_states.get(user_id, {})
        amount = data.get('amount')
        commission = data.get('commission')
        total = data.get('total')
        available = get_available_balance(user_id)

        if balance < total:
            shortage = total - balance
            logging.warning(f"[COMPANY][{user_id}] رصيد غير كافٍ (balance={balance}, total={total})")
            kb = make_inline_buttons(
                ("💳 شحن المحفظة", "recharge_wallet"),
                ("⬅️ رجوع", "company_commission_cancel")
            )
            bot.edit_message_text(
                f"❌ يا {name}، رصيدك مش مكفي.\n"
                f"المطلوب: {total:,} ل.س\n"
                f"متاحك الحالي: {balance:,} ل.س\n"
                f"الناقص: {shortage:,} ل.س\n"
                "اشحن محفظتك أو ارجع خطوة وغيّر المبلغ.",
                call.message.chat.id, call.message.message_id,
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
            bot.edit_message_text(
                f"⚠️ يا {name}، حصلت مشكلة بسيطة وإحنا بنثبت قيمة العملية. جرّب تاني بعد شوية أو كلّمنا لو استمرت.",
                call.message.chat.id, call.message.message_id
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
            f"✅ الإجمالي (محجوز): {total:,} ل.س\n\n"
            f"يمكنك الرد برسالة أو صورة ليصل للعميل."
        )

        bot.edit_message_text(
            f"✅ تمام يا {name} — طلبك اتبعت للإدارة. هنراجع ونرجعلك إشعار بالتنفيذ قريبًا.",
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
                "hold_id": hold_id,    # ✅ أهم حاجة
            }
        )
        bot.send_message(
            user_id,
            f"📝 يا {name}، طلبك اتسجّل في الطابور.\n"
            "لما الأدمن يأكّد، الحجز بيتصفّى وبتوصلك رسالة التنفيذ.\n"
            "لو اترفض، بنفكّ الحجز وبنرجّع الفلوس فورًا."
        )
        process_queue(bot)

    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet")
    def show_recharge_methods(call):
        user_id = call.from_user.id
        name = _user_name(bot, user_id)
        bot.send_message(call.message.chat.id, f"💳 يا {name}، اختار طريقة شحن محفظتك:", reply_markup=keyboards.recharge_menu())

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
                # fallback قديم: خصم يدوي
                if not has_sufficient_balance(user_id, reserved):
                    logging.warning(f"[COMPANY][ADMIN][{user_id}] رصيد غير كافٍ")
                    bot.send_message(user_id, "❌ فشل الحوالة: رصيدك مش مكفي.")
                    bot.answer_callback_query(call.id, "❌ رصيد العميل مش مكفي.")
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                    return
                deduct_balance(user_id, reserved)

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
                f"✅ تمام يا {name}! تم تنفيذ الحوالة عبر {company} للمستفيد {beneficiary_name} بمبلغ {amount:,} ل.س."
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

            logging.info(f"[COMPANY][ADMIN][{user_id}] تم رفض الحوالة")
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
