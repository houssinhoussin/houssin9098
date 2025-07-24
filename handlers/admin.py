from services.queue_service import add_pending_request, process_queue, delete_pending_request, postpone_request, queue_cooldown_start
import logging
import json
import os
import re
from datetime import datetime

from telebot import types

from config import ADMINS, ADMIN_MAIN_ID
from database.db import get_table
from services.wallet_service import (
    register_user_if_not_exist,
    deduct_balance,
    add_purchase,
    add_balance,
    get_balance,
)
from services.cleanup_service import delete_inactive_users
from services.recharge_service import validate_recharge_code

from handlers.products import pending_orders  # هام

from handlers import cash_transfer
from handlers import companies_transfer

SECRET_CODES_FILE = "data/secret_codes.json"
os.makedirs("data", exist_ok=True)
if not os.path.isfile(SECRET_CODES_FILE):
    with open(SECRET_CODES_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

def load_code_operations():
    with open(SECRET_CODES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_code_operations(data):
    with open(SECRET_CODES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

VALID_SECRET_CODES = [
    "363836369", "36313251", "646460923",
    "91914096", "78708501", "06580193"
]

def clear_pending_request(user_id):
    try:
        from handlers.recharge import recharge_pending
        recharge_pending.discard(user_id)
    except Exception:
        pass

_cancel_pending = {}
_accept_pending = {}

def register(bot, history):
    # تسجيل هاندلرات التحويلات الجديدة
    cash_transfer.register(bot, history)
    companies_transfer.register_companies_transfer(bot, history)

    @bot.message_handler(func=lambda msg: msg.text and re.match(r'/done_(\d+)', msg.text))
    def handle_done(msg):
        req_id = int(re.match(r'/done_(\d+)', msg.text).group(1))
        delete_pending_request(req_id)
        bot.reply_to(msg, f"✅ تم إنهاء الطلب {req_id}")

    @bot.message_handler(func=lambda msg: msg.text and re.match(r'/cancel_(\d+)', msg.text))
    def handle_cancel(msg):
        req_id = int(re.match(r'/cancel_(\d+)', msg.text).group(1))
        delete_pending_request(req_id)
        bot.reply_to(msg, f"🚫 تم إلغاء الطلب {req_id}")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_queue_"))
    def handle_queue_action(call):
        parts = call.data.split("_")
        action = parts[2]
        request_id = int(parts[3])

        # Fetch request with payload
        res = get_table("pending_requests") \
            .select("user_id", "request_text", "payload") \
            .eq("id", request_id) \
            .execute()
        if not getattr(res, 'data', None):
            return bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
        req = res.data[0]
        user_id = req["user_id"]
        payload = req.get("payload") or {}

        # Remove admin message
        bot.delete_message(call.message.chat.id, call.message.message_id)

        if action == "postpone":
            postpone_request(request_id)
            bot.answer_callback_query(call.id, "✅ تم تأجيل الطلب.")
            bot.send_message(user_id, "⏳ نعتذر؛ طلبك أعيد إلى نهاية القائمة.")
            # لا نقوم بجدولة جديدة هنا لأن التأجيل فعلٌ بحد ذاته
            return

        elif action == "cancel":
            delete_pending_request(request_id)
            reserved = payload.get("reserved", 0)
            if reserved:
                add_balance(user_id, reserved)
                bot.send_message(user_id, f"🚫 تم إلغاء طلبك واسترجاع {reserved:,} ل.س.")
     
            bot.answer_callback_query(call.id, "🚫 تم إلغاء الطلب.")
            bot.answer_callback_query(call.id, "✅ تم إلغاء الطلب.")
            return

        elif action == "accept":
            typ = payload.get("type")
            if typ in ("syr_unit", "mtn_unit"):
                price = payload.get("price", 0)
                num   = payload.get("number")
                name  = payload.get("unit_name")
                # تسجيل الشراء (الخصم تم مسبقًا عند الإرسال)
                add_purchase(user_id, price, name, price, num)
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم تحويل {name} بنجاح إلى {num}.\nتم خصم {price:,} ل.س.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                return

            elif typ in ("syr_bill", "mtn_bill"):
                reserved = payload.get("reserved", 0)
                num = payload.get("number")
                label = "فاتورة سيرياتيل" if typ == "syr_bill" else "فاتورة MTN"
                # لا نخصم مرة ثانية لأن الحجز تم مسبقًا
                add_purchase(user_id, reserved, label, reserved, num)
                bot.send_message(
                    user_id,
                    f"✅ تم دفع {label} للرقم {num}.\n"
                    f"تم خصم {reserved:,} ل.س.",
                    parse_mode="HTML"
                )
                delete_pending_request(request_id)
            elif typ == "internet":
                reserved  = payload.get("reserved", 0)
                provider  = payload.get("provider")
                speed     = payload.get("speed")
                phone     = payload.get("phone")
                print(f"[DEBUG] Accepting internet order: reserved={reserved}, provider={provider}, speed={speed}, phone={phone}")
                # لا نخصم مرة ثانية لأن الحجز تم مسبقًا
                add_purchase(user_id, reserved, f"إنترنت {provider} {speed}", reserved, phone)
                bot.send_message(
                    user_id,
                    f"✅ تم شحن إنترنت {provider} بسرعة {speed} للرقم {phone}.\n"
                    f"تم خصم {reserved:,} ل.س.",
                    parse_mode="HTML"
                )
                delete_pending_request(request_id)
            elif typ == "cash_transfer":
                reserved = payload.get("reserved", 0)
                number = payload.get("number")
                cash_type = payload.get("cash_type")
                add_purchase(user_id, reserved, f"تحويل كاش {cash_type}", reserved, number)
                bot.send_message(
                    user_id,
                    f"✅ تم تنفيذ تحويل كاش {cash_type} للرقم {number}.\nتم خصم {reserved:,} ل.س.",
                    parse_mode="HTML"
                )
                delete_pending_request(request_id)
            elif typ == "companies_transfer":
                reserved = payload.get("reserved", 0)
                beneficiary_name = payload.get("beneficiary_name")
                beneficiary_number = payload.get("beneficiary_number")
                company = payload.get("company")
                add_purchase(user_id, reserved, f"حوالة مالية عبر {company}", reserved, beneficiary_number)
                bot.send_message(
                    user_id,
                    f"✅ تم تنفيذ حوالة مالية عبر {company} للمستفيد {beneficiary_name}.\nتم خصم {reserved:,} ل.س.",
                    parse_mode="HTML"
                )
                delete_pending_request(request_id)
            else:
                bot.answer_callback_query(call.id, "❌ نوع الطلب غير معروف.")
                return

            bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
            queue_cooldown_start(bot)

        else:
            bot.answer_callback_query(call.id, "❌ حدث خطأ.")

        # الإجرائات الإضافية لو تكررت الأكشنات، احفظها ضمن else لو لزم الأمر
        if action == "cancel":
            bot.answer_callback_query(call.id, "🚫 يرجى كتابة سبب الإلغاء أو إرسال صورة (سيتم إرساله للعميل):")
            _cancel_pending[call.from_user.id] = {"request_id": request_id, "user_id": user_id}
            bot.send_message(call.message.chat.id, "✏️ أرسل سبب الإلغاء كتابياً أو أرسل صورة للعميل:")
            bot.register_next_step_handler_by_chat_id(
                call.message.chat.id,
                lambda msg: handle_cancel_reason(msg, call)
            )

        elif action == "accept":
            # استخراج السعر والمنتج وplayer_id من نص الطلب
            text = req.get("request_text", "")
            m_price = re.search(r"💵 السعر: ([\d,]+) ل\.س", text)
            price = int(m_price.group(1).replace(",", "")) if m_price else 0
            m_prod = re.search(r"🔖 المنتج: (.+)", text)
            product_name = m_prod.group(1) if m_prod else ""
            m_player = re.search(r"آيدي اللاعب: <code>(.+?)</code>", text)
            player_id = m_player.group(1) if m_player else ""

            # حذف الطلب
            delete_pending_request(request_id)
            bot.answer_callback_query(call.id, "✅ تم قبول وتنفيذ الطلب.")

            # إعلام العميل أن الطلب تم تنفيذه مع الخصم
            bot.send_message(
                user_id,
                f"✅ تم تنفيذ طلبك: {product_name}\nتم خصم {price:,} ل.س من محفظتك."
            )

            _accept_pending[call.from_user.id] = user_id
            bot.send_message(call.message.chat.id, "✉️ أرسل رسالة للعميل أو صورة (أرسل /skip لتخطي):")
            bot.register_next_step_handler_by_chat_id(
                call.message.chat.id,
                lambda msg: handle_accept_message(msg, call)
            )
            pending_orders.discard(user_id)
            queue_cooldown_start(bot)

        elif action == "message":
            _accept_pending[call.from_user.id] = user_id
            bot.send_message(call.message.chat.id, "✉️ أرسل الرسالة للعميل:")
            bot.register_next_step_handler_by_chat_id(
                call.message.chat.id,
                lambda msg: handle_accept_message(msg, call)
            )
        elif action == "photo":
            _accept_pending[call.from_user.id] = user_id
            bot.send_message(call.message.chat.id, "🖼️ أرسل الصورة للعميل:")
            bot.register_next_step_handler_by_chat_id(
                call.message.chat.id,
                lambda msg: handle_accept_message(msg, call)
            )

        else:
            bot.answer_callback_query(call.id, "❌ حدث خطأ غير متوقع.")

    def handle_cancel_reason(msg, call):
        data = _cancel_pending.get(msg.from_user.id)
        if not data:
            return
        user_id = data["user_id"]
        request_id = data["request_id"]
        if msg.content_type == 'text':
            reason_text = msg.text.strip()
            bot.send_message(
                user_id,
                f"❌ تم إلغاء طلبك من الإدارة.\n📝 السبب: {reason_text}"
            )
        elif msg.content_type == 'photo':
            bot.send_photo(user_id, msg.photo[-1].file_id, caption="❌ تم إلغاء طلبك من الإدارة.")
        else:
            bot.send_message(user_id, "❌ تم إلغاء طلبك من الإدارة.")
        bot.send_message(msg.chat.id, "تم إرسال سبب الإلغاء للعميل وحذف الطلب.")
        delete_pending_request(request_id)
        pending_orders.discard(user_id)
        queue_cooldown_start(bot)
        _cancel_pending.pop(msg.from_user.id, None)

    def handle_accept_message(msg, call):
        user_id = _accept_pending.get(msg.from_user.id)
        if not user_id:
            return
        if msg.text and msg.text.strip() == "/skip":
            bot.send_message(msg.chat.id, "✅ تم تخطي إرسال رسالة للعميل.")
        elif msg.content_type == "text":
            bot.send_message(user_id, f"📩 رسالة من الإدارة:\n{msg.text.strip()}")
            bot.send_message(msg.chat.id, "✅ تم إرسال الرسالة للعميل.")
        elif msg.content_type == "photo":
            bot.send_photo(user_id, msg.photo[-1].file_id, caption="📩 صورة من الإدارة.")
            bot.send_message(msg.chat.id, "✅ تم إرسال الصورة للعميل.")
        else:
            bot.send_message(msg.chat.id, "❌ نوع الرسالة غير مدعوم.")
        _accept_pending.pop(msg.from_user.id, None)

    # ========== شحن المحفظة ==========
    @bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_add_"))
    def confirm_wallet_add(call):
        _, _, user_id_str, amount_str = call.data.split("_")
        user_id = int(user_id_str)
        amount = int(float(amount_str))
        register_user_if_not_exist(user_id)
        add_balance(user_id, amount)
        clear_pending_request(user_id)
        bot.send_message(user_id, f"✅ تم إضافة {amount:,} ل.س إلى محفظتك بنجاح.")
        bot.answer_callback_query(call.id, "✅ تمت الموافقة")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("reject_add_"))
    def reject_wallet_add(call):
        user_id = int(call.data.split("_")[-1])
        bot.send_message(call.message.chat.id, "📝 اكتب سبب الرفض:")
        bot.register_next_step_handler_by_chat_id(
            call.message.chat.id,
            lambda m: process_rejection(m, user_id, call),
        )

    def process_rejection(msg, user_id, call):
        reason = msg.text.strip()
        bot.send_message(
            user_id,
            f"❌ تم رفض عملية الشحن.\n📝 السبب: {reason}"
        )
        bot.answer_callback_query(call.id, "❌ تم رفض العملية")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        clear_pending_request(user_id)

  
