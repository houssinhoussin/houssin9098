# handlers/admin.py

import re
import logging
from datetime import datetime
from telebot import types

from config import ADMINS, ADMIN_MAIN_ID
from database.db import get_table

# طابور الطلبات
from services.queue_service import (
    add_pending_request,
    process_queue,
    delete_pending_request,
    postpone_request,
    queue_cooldown_start,
)

# محفظة/مشتريات
from services.wallet_service import (
    register_user_if_not_exist,
    deduct_balance,
    add_purchase,
    add_balance,
    get_balance,
)

# تنظيف مستخدمين
from services.cleanup_service import delete_inactive_users

# تحويلات
from handlers import cash_transfer, companies_transfer

# إعلانات القناة
from services.ads_service import add_channel_ad

# ---- تخزين الحالة في Supabase بدلاً من dict في الذاكرة ----
from services.state_service import set_state, get_state, delete_state
# ---- توحيد retry/backoff لاستعلامات Supabase الحرجة ----
from utils.retry import retry
import httpx

# مفتاح حالة جلسة مراسلة العميل من الأدمن
ADMIN_MSG_KEY = "admin_msg_session"

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

    # ────────────────────────────────────────────────
    #  أزرار ✉️ رسالة للعميل / 🖼️ صورة للعميل
    # ────────────────────────────────────────────────

    @retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="fetch pending user_id")
    def _fetch_pending_user_id(request_id: int):
        return get_table("pending_requests").select("user_id").eq("id", request_id).limit(1).execute()

    @bot.callback_query_handler(func=lambda c: c.data.startswith("admin_queue_message_"))
    def cb_queue_message(c: types.CallbackQuery):
        request_id = int(c.data.split("_")[3])
        res = _fetch_pending_user_id(request_id)
        if not res.data:
            return bot.answer_callback_query(c.id, "❌ الطلب غير موجود.")
        target_uid = res.data[0]["user_id"]

        # خزّن جلسة مراسلة العميل للأدمن في Supabase مع TTL = 10 دقائق
        set_state(c.from_user.id, ADMIN_MSG_KEY, {"user_id": target_uid, "mode": "text"}, ttl_seconds=600)
        bot.answer_callback_query(c.id)
        bot.send_message(c.from_user.id, "📝 اكتب الرسالة الآن (أو /cancel لإلغاء).")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("admin_queue_photo_"))
    def cb_queue_photo(c: types.CallbackQuery):
        request_id = int(c.data.split("_")[3])
        res = _fetch_pending_user_id(request_id)
        if not res.data:
            return bot.answer_callback_query(c.id, "❌ الطلب غير موجود.")
        target_uid = res.data[0]["user_id"]

        # خزّن جلسة إرسال صورة مع TTL = 10 دقائق
        set_state(c.from_user.id, ADMIN_MSG_KEY, {"user_id": target_uid, "mode": "photo"}, ttl_seconds=600)
        bot.answer_callback_query(c.id)
        bot.send_message(c.from_user.id, "📷 أرسل الصورة الآن (أو /cancel لإلغاء).")

    # ✅ تعديل الفلتر: للأدمن فقط (كي لا ينافس هاندلرات المستخدمين مثل زر المنتجات)
    @bot.message_handler(
        func=lambda m: (hasattr(m, "from_user") and m.from_user and m.from_user.id in ADMINS),
        content_types=["text", "photo"]
    )
    def forward_to_client(m: types.Message):
        """
        يستقبل رسالة/صورة من الأدمن فقط، ويتحقق إن عنده جلسة مراسلة فعّالة محفوظة.
        """
        # اقرأ حالة جلسة الأدمن
        sess = get_state(m.from_user.id, ADMIN_MSG_KEY)
        if not sess:
            return  # لا توجد جلسة مراسلة نشطة لهذا الأدمن

        uid = sess.get("user_id")
        mode = sess.get("mode")

        # إنهاء الجلسة عند /cancel
        if m.text and m.text.strip() == "/cancel":
            delete_state(m.from_user.id, ADMIN_MSG_KEY)
            return bot.reply_to(m, "❎ تم إلغاء الجلسة.")

        # إرسال حسب الوضع المحفوظ
        if mode == "text":
            if m.content_type != "text":
                return bot.reply_to(m, "❌ المطلوب نص فقط.")
            bot.send_message(uid, m.text)
        else:  # mode == photo
            if m.content_type != "photo":
                return bot.reply_to(m, "❌ المطلوب صورة فقط.")
            bot.send_photo(uid, m.photo[-1].file_id, caption=(m.caption or ""))

        # نظّف الجلسة بعد الإرسال
        delete_state(m.from_user.id, ADMIN_MSG_KEY)
        bot.reply_to(m, "✅ أُرسلت للعميل. يمكنك الآن الضغط «تأكيد» أو «إلغاء».")

    # ---- استعلام الطلب في إجراء (مع retry) ----
    @retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="fetch pending request")
    def _fetch_pending_request(request_id: int):
        return (
            get_table("pending_requests")
            .select("user_id, request_text, payload")
            .eq("id", request_id)
            .limit(1)
            .execute()
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("admin_queue_"))
    def handle_queue_action(call):
        parts      = call.data.split("_")
        action     = parts[2]
        request_id = int(parts[3])

        # جلب الطلب (مع retry/backoff)
        res = _fetch_pending_request(request_id)
        if not getattr(res, "data", None):
            return bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
        req      = res.data[0]
        user_id  = req["user_id"]
        payload  = req.get("payload") or {}

        # حذف رسالة الأدمن
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            logging.exception("Failed to delete admin message")

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
            amount = payload.get("reserved", payload.get("price", 0))
            if amount:
                add_balance(user_id, amount)

            typ = payload.get("type")

            if typ == "order":
                reserved   = payload.get("reserved", 0)
                if reserved:
                    add_balance(user_id, reserved)
                reserved   = payload.get("reserved", 0)

                product_id = payload.get("product_id")
                player_id  = payload.get("player_id")
                name       = f"طلب منتج #{product_id}"

                add_purchase(user_id, reserved, name, reserved, player_id)
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
                label     = payload.get("unit_name", f"فاتورة")
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
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم دفع فاتورة الإنترنت ({provider}) بسرعة {speed} لرقم `{phone}` بنجاح.\n"
                    f"تم خصم {reserved:,} ل.س من محفظتك.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

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
                amount    = payload.get("amount", 0)
                add_balance(user_id, amount)
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم شحن محفظتك بمبلغ {amount:,} ل.س بنجاح."
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ عملية الشحن")
                queue_cooldown_start(bot)
                return

            elif typ == "ads":
                reserved = payload.get("reserved", payload.get("price", 0))
                count    = payload.get("count", 1)
                contact  = payload.get("contact", "")
                ad_text  = payload.get("ad_text", "")
                images   = payload.get("images", [])

                if reserved:
                    deduct_balance(user_id, reserved)

                add_channel_ad(user_id, count, reserved, contact, ad_text, images)
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تم قبول إعلانك وسيتم نشره في القناة حسب الجدولة.\n"
                    f"تم خصم {reserved:,} ل.س.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم قبول الإعلان")
                queue_cooldown_start(bot)
                return

            else:
                return bot.answer_callback_query(call.id, "❌ نوع الطلب غير معروف.")

        # أيّ أكشن آخر
        bot.answer_callback_query(call.id, "❌ حدث خطأ غير متوقع.")
