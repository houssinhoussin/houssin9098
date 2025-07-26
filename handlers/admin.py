# admin.py

import re
import logging
from datetime import datetime
from telebot import types
from services.ads_service import add_channel_ad
from config import ADMINS, ADMIN_MAIN_ID
from database.db import get_table
from services.queue_service import (
    add_pending_request,
    process_queue,
    delete_pending_request,
    postpone_request,
    queue_cooldown_start,
)
from services.wallet_service import (
    register_user_if_not_exist,
    deduct_balance,
    add_purchase,
    add_balance,
    get_balance,
)
from services.cleanup_service import delete_inactive_users
from handlers import cash_transfer, companies_transfer
from services.ads_service import add_channel_ad

_cancel_pending = {}
_accept_pending = {}

def register(bot, history):
    # تسجيل الهاندلرات للتحويلات
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
        parts      = call.data.split("_")
        action     = parts[2]
        request_id = int(parts[3])

        # جلب الطلب
        res = (
            get_table("pending_requests")
            .select("user_id", "request_text", "payload")
            .eq("id", request_id)
            .execute()
        )
        if not getattr(res, "data", None):
            return bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
        req      = res.data[0]
        user_id  = req["user_id"]
        payload  = req.get("payload") or {}

        # حذف رسالة الأدمن
        bot.delete_message(call.message.chat.id, call.message.message_id)

        # === تأجيل الطلب ===
        if action == "postpone":
            postpone_request(request_id)
            bot.send_message(user_id, "⏳ نعتذر؛ طلبك أعيد إلى نهاية القائمة.")
            bot.answer_callback_query(call.id, "✅ تم تأجيل الطلب.")
            queue_cooldown_start(bot)
            return

        # === إلغاء الطلب ===
        if action == "cancel":
            delete_pending_request(request_id)
            reserved = payload.get("reserved", 0)
            if reserved:
                add_balance(user_id, reserved)
                bot.send_message(user_id, f"🚫 تم استرجاع {reserved:,} ل.س إلى محفظتك.")
            bot.answer_callback_query(call.id, "✅ تم إلغاء الطلب.")
            queue_cooldown_start(bot)
            return

        # === قبول الطلب ===
        if action == "accept":
            # ==== إعادة المبلغ المحجوز قبل تسجيل الشراء لمنع الخصم المزدوج ====
            amount = payload.get("reserved", payload.get("price", 0))
            if amount:
                add_balance(user_id, amount)
            typ = payload.get("type")
            # ——— طلبات المنتجات الرقمية ———
            if typ == "order":
                reserved   = payload.get("reserved", 0)
                # لا تعيد الحجز هنا!
                if reserved:
                    add_balance(user_id, reserved)
                reserved   = payload.get("reserved", 0)

                product_id = payload.get("product_id")
                player_id  = payload.get("player_id")
                name       = f"طلب منتج #{product_id}"

                # ثمّ تسجّل الشراء
                add_purchase(user_id, reserved, name, reserved, player_id)
                # سجّل الشراء (الخصم تمّ فعليّاً عند الإرسال)
                add_purchase(user_id, reserved, name, reserved, player_id)

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم تنفيذ طلبك: {name}\nتم خصم {reserved:,} ل.س من محفظتك.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            if typ in ("syr_unit", "mtn_unit"):
                price = payload.get("price", 0)
                num   = payload.get("number")
                name  = payload.get("unit_name")
                add_purchase(user_id, price, name, price, num)
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم تنفيذ عملية تحويل الوحدات بنجاح!\n"
                    f"• الرقم: <code>{num}</code>\n"
                    f"• الكمية: {name}\n"
                    f"• السعر: {price:,} ل.س",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            elif typ in ("syr_bill", "mtn_bill"):
                reserved  = payload.get("reserved", 0)
                num       = payload.get("number")
                label     = payload.get("unit_name", f"فاتورة")  # أو اسم خاص لو كان موجودًا
                add_purchase(user_id, reserved, label, reserved, num)
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم دفع الفاتورة بنجاح!\n"
                    f"• الرقم: <code>{num}</code>\n"
                    f"• المبلغ: {reserved:,} ل.س",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            elif typ == "internet":
                reserved = payload.get("reserved", 0)
                provider = payload.get("provider")
                speed    = payload.get("speed")
                phone    = payload.get("phone")
                add_purchase(user_id, reserved, f"إنترنت {provider} {speed}", reserved, phone)

            elif typ == "cash_transfer":
                reserved  = payload.get("reserved", 0)
                number    = payload.get("number")
                cash_type = payload.get("cash_type")
                add_purchase(user_id, reserved, f"تحويل كاش {cash_type}", reserved, number)

            elif typ == "companies_transfer":
                reserved           = payload.get("reserved", 0)
                beneficiary_name   = payload.get("beneficiary_name")
                beneficiary_number = payload.get("beneficiary_number")
                company            = payload.get("company")
                add_purchase(
                    user_id,
                    reserved,
                    f"حوالة مالية عبر {company}",
                    reserved,
                    beneficiary_number,
                )

                delete_pending_request(request_id)
                amount = payload.get("reserved", payload.get("price", 0))
                bot.send_message(
                    user_id,
                    f"✅ تم تنفيذ طلبك بنجاح.\nتم خصم {amount:,} ل.س.",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            elif typ == "university_fees":
                reserved      = payload.get("reserved", 0)
                university    = payload.get("university")
                national_id   = payload.get("national_id")
                university_id = payload.get("university_id")
                amount        = payload.get("amount")
                commission    = payload.get("commission")
                total         = payload.get("total")

                add_purchase(
                    user_id,
                    reserved,
                    f"دفع رسوم جامعية ({university})",
                    reserved,
                    university_id
                )

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم دفع رسومك الجامعية ({university}) بمبلغ {reserved:,} ل.س بنجاح."
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            elif typ == "recharge":
                amount = payload.get("amount", 0)
                photo_id = payload.get("photo")  # ← جلب file_id للصورة من البايلود

                # أرسل الصورة للأدمن أولاً مع نص الطلب إذا الصورة موجودة
                if photo_id:
                    bot.send_photo(
                        call.message.chat.id,
                        photo_id,
                        caption=f"💳 طلب شحن محفظة\n"
                                f"المستخدم: {user_id}\n"
                                f"المبلغ: {amount:,} ل.س\n"
                                f"اسم المستخدم: @{req.get('username','-')}\n"
                                f"ID: {user_id}"
                    )
                else:
                    bot.send_message(
                        call.message.chat.id,
                        f"💳 طلب شحن محفظة\n"
                        f"المستخدم: {user_id}\n"
                        f"المبلغ: {amount:,} ل.س\n"
                        f"(بدون صورة)"
                    )

                # تنفيذ عملية الشحن للمستخدم كالمعتاد
                delete_pending_request(request_id)
                add_balance(user_id, amount)
                bot.send_message(
                    user_id,
                    f"✅ تم شحن محفظتك بمبلغ {amount:,} ل.س بنجاح."
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ عملية الشحن")
                queue_cooldown_start(bot)
                return
                
            elif typ == "ads":
                count    = payload.get("count", 1)
                price    = payload.get("price", 0)
                contact  = payload.get("contact", "")
                ad_text  = payload.get("ad_text", "")
                images   = payload.get("images", [])
                add_channel_ad(user_id, count, price, contact, ad_text, images)
                # بعد إضافة الإعلان للجدول يتم إعلام العميل
                delete_pending_request(request_id)
                bot.send_message(user_id, "✅ تم قبول إعلانك وسيتم نشره في القناة حسب الجدولة.")
                bot.answer_callback_query(call.id, "✅ تم قبول الإعلان")
                queue_cooldown_start(bot)
                return
            else:
                return bot.answer_callback_query(call.id, "❌ نوع الطلب غير معروف.")
                
        # أيّ أكشن آخر
        bot.answer_callback_query(call.id, "❌ حدث خطأ غير متوقع.")

    def handle_cancel_reason(msg, call):
        data = _cancel_pending.get(msg.from_user.id)
        if not data:
            return
        user_id    = data["user_id"]
        request_id = data["request_id"]
        if msg.content_type == "text":
            reason_text = msg.text.strip()
            bot.send_message(
                user_id,
                f"❌ تم إلغاء طلبك من الإدارة.\n📝 السبب: {reason_text}",
            )
        elif msg.content_type == "photo":
            bot.send_photo(
                user_id,
                msg.photo[-1].file_id,
                caption="❌ تم إلغاء طلبك من الإدارة.",
            )
        else:
            bot.send_message(user_id, "❌ تم إلغاء طلبك من الإدارة.")
        delete_pending_request(request_id)
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
            bot.send_photo(
                user_id,
                msg.photo[-1].file_id,
                caption="📩 صورة من الإدارة.",
            )
            bot.send_message(msg.chat.id, "✅ تم إرسال الصورة للعميل.")
        else:
            bot.send_message(msg.chat.id, "❌ نوع الرسالة غير مدعوم.")
        _accept_pending.pop(msg.from_user.id, None)

