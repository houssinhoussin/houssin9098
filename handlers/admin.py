# admin.py

import re
import logging
from datetime import datetime
from telebot import types
from services.ads_service import add_channel_ad
from config import ADMINS, ADMIN_MAIN_ID
from database.db import get_table
from services.products_admin import set_product_active
from services.report_service import totals_deposits_and_purchases_syp, pending_queue_count, summary
from services.system_service import set_maintenance, is_maintenance, maintenance_message, get_logs_tail, force_sub_recheck
from services.activity_logger import log_action
from services.authz import allowed
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
    # ✅ إضافات write-through للجداول المتخصصة
    add_bill_or_units_purchase,
    add_internet_purchase,
    add_cash_transfer_purchase,
    add_companies_transfer_purchase,
    add_university_fees_purchase,
    add_ads_purchase,
)
from services.cleanup_service import delete_inactive_users
from handlers import cash_transfer, companies_transfer
from services.ads_service import add_channel_ad

_cancel_pending = {}
_accept_pending = {}
_msg_pending = {}

def register(bot, history):
    # تسجيل الهاندلرات للتحويلات
    cash_transfer.register(bot, history)
    companies_transfer.register_companies_transfer(bot, history)

    @bot.message_handler(func=lambda msg: msg.text and re.match(r'/done_(\d+)', msg.text) and msg.from_user.id in ADMINS)
    def handle_done(msg):
        req_id = int(re.match(r'/done_(\d+)', msg.text).group(1))
        delete_pending_request(req_id)
        bot.reply_to(msg, f"✅ تم إنهاء الطلب {req_id}")

    @bot.message_handler(func=lambda msg: msg.text and re.match(r'/cancel_(\d+)', msg.text) and msg.from_user.id in ADMINS)
    def handle_cancel(msg):
        req_id = int(re.match(r'/cancel_(\d+)', msg.text).group(1))
        delete_pending_request(req_id)
        bot.reply_to(msg, f"🚫 تم إلغاء الطلب {req_id}")

    # ────────────────────────────────────────────────
    #  أزرار ✉️ رسالة للعميل / 🖼️ صورة للعميل
    # ────────────────────────────────────────────────

    @bot.callback_query_handler(func=lambda c: (c.data.startswith("admin_queue_message_")) and c.from_user.id in ADMINS)
    def cb_queue_message(c: types.CallbackQuery):
        if not allowed(c.from_user.id, 'queue:message'):
            return bot.answer_callback_query(c.id, '❌ ليس لديك صلاحية.')
        request_id = int(c.data.split("_")[3])
        res = get_table("pending_requests").select("user_id").eq("id", request_id).execute()
        if not res.data:
            return bot.answer_callback_query(c.id, "❌ الطلب غير موجود.")
        _msg_pending[c.from_user.id] = {"user_id": res.data[0]["user_id"], "mode": "text"}
        bot.answer_callback_query(c.id)
        bot.send_message(c.from_user.id, "📝 اكتب الرسالة الآن (أو /cancel لإلغاء).")

    @bot.callback_query_handler(func=lambda c: (c.data.startswith("admin_queue_photo_")) and c.from_user.id in ADMINS)
    def cb_queue_photo(c: types.CallbackQuery):
        if not allowed(c.from_user.id, 'queue:photo'):
            return bot.answer_callback_query(c.id, '❌ ليس لديك صلاحية.')
        request_id = int(c.data.split("_")[3])
        res = get_table("pending_requests").select("user_id").eq("id", request_id).execute()
        if not res.data:
            return bot.answer_callback_query(c.id, "❌ الطلب غير موجود.")
        _msg_pending[c.from_user.id] = {"user_id": res.data[0]["user_id"], "mode": "photo"}
        bot.answer_callback_query(c.id)
        bot.send_message(c.from_user.id, "📷 أرسل الصورة الآن (أو /cancel لإلغاء).")

    @bot.message_handler(func=lambda m: m.from_user.id in _msg_pending,
                         content_types=["text", "photo"])
    def forward_to_client(m: types.Message):
        data = _msg_pending.pop(m.from_user.id)            # نحصل ثم نحذف الجلسة
        uid  = data["user_id"]
        if data["mode"] == "text":
            if m.content_type != "text":
                return bot.reply_to(m, "❌ المطلوب نص فقط.")
            bot.send_message(uid, m.text)
        else:  # mode == photo
            if m.content_type != "photo":
                return bot.reply_to(m, "❌ المطلوب صورة فقط.")
            bot.send_photo(uid, m.photo[-1].file_id, caption=m.caption or "")
        bot.reply_to(m, "✅ أُرسلت للعميل. يمكنك الآن الضغط «تأكيد» أو «إلغاء».")

    @bot.callback_query_handler(func=lambda call: (call.data.startswith("admin_queue_")) and call.from_user.id in ADMINS)
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
            if not allowed(call.from_user.id, "queue:cancel"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            postpone_request(request_id)
            bot.send_message(user_id, "⏳ نعتذر؛ طلبك أعيد إلى نهاية القائمة.")
            bot.answer_callback_query(call.id, "✅ تم تأجيل الطلب.")
            queue_cooldown_start(bot)
            return

        # === إلغاء الطلب ===
        if action == "cancel":
            if not allowed(call.from_user.id, "queue:cancel"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
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
                # (لا يوجد جدول متخصص هنا)

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
                # ✅ كتابة إضافية في جدول bill_and_units_purchases
                try:
                    add_bill_or_units_purchase(user_id, bill_name=name, price=price, number=str(num))
                except Exception:
                    pass

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
                # ✅ كتابة إضافية في جدول bill_and_units_purchases
                try:
                    add_bill_or_units_purchase(user_id, bill_name=label, price=reserved, number=str(num))
                except Exception:
                    pass

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

                # خصم نهائي (add_purchase يخصم داخلياً)
                add_purchase(user_id, reserved, f"إنترنت {provider} {speed}", reserved, phone)
                # ✅ كتابة إضافية في جدول internet_providers_purchases
                try:
                    add_internet_purchase(user_id, provider_name=provider, price=reserved, phone=str(phone), speed=speed)
                except Exception:
                    pass

                # حذف الطلب من الطابور
                delete_pending_request(request_id)

                # إشعار العميل
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
                # ✅ كتابة إضافية في جدول cash_transfer_purchases
                try:
                    add_cash_transfer_purchase(user_id, transfer_name=f"تحويل كاش {cash_type}", price=reserved, number=str(number))
                except Exception:
                    pass
                # (باقي المنطق كما هو لديك)

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
                # ✅ كتابة إضافية في جدول companies_transfer_purchases
                try:
                    add_companies_transfer_purchase(user_id, company_name=company, price=reserved, beneficiary_number=str(beneficiary_number))
                except Exception:
                    pass

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
                # ✅ كتابة إضافية في جدول university_fees_purchases
                try:
                    add_university_fees_purchase(user_id, university_name=university, price=reserved, university_id=str(university_id))
                except Exception:
                    pass

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

                # تنفيذ عملية الشحن
                add_balance(user_id, amount)

                # حذف الطلب من الطابور
                delete_pending_request(request_id)

                # إعلام المستخدم
                bot.send_message(
                    user_id,
                    f"✅ تم شحن محفظتك بمبلغ {amount:,} ل.س بنجاح."
                )

                bot.answer_callback_query(call.id, "✅ تم تنفيذ عملية الشحن")
                queue_cooldown_start(bot)
                return
              
            elif typ == "ads":
                if not allowed(call.from_user.id, "ads:post"):
                    return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية نشر إعلان.")
                reserved = payload.get("reserved", payload.get("price", 0))
                count    = payload.get("count", 1)
                contact  = payload.get("contact", "")
                ad_text  = payload.get("ad_text", "")
                images   = payload.get("images", [])

                # خصم نهائي للمبلغ (بعد استرجاع الحجز أعلاه)
                if reserved:
                    deduct_balance(user_id, reserved)

                # إدراج الإعلان في جدول القناة
                add_channel_ad(user_id, count, reserved, contact, ad_text, images)
                # ✅ كتابة إضافية في جدول ads_purchases
                try:
                    add_ads_purchase(user_id, ad_name="إعلان مدفوع", price=reserved)
                except Exception:
                    pass

                # حذف الطلب من الطابور
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

    # ===== تسجيل هاندلرات قائمة الأدمن (داخل register بحيث يتوفر bot) =====

    @bot.message_handler(commands=['admin'])
    def admin_menu(msg):
        if msg.from_user.id not in ADMINS:
            return bot.reply_to(msg, "صلاحية الأدمن فقط.")
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🛒 إدارة المنتجات", "📊 تقارير سريعة", "⏳ طابور الانتظار")
        kb.row("⚙️ النظام", "⬅️ رجوع")
        bot.send_message(msg.chat.id, "لوحة الأدمن:", reply_markup=kb)

    @bot.message_handler(func=lambda m: m.text == "🛒 إدارة المنتجات" and m.from_user.id in ADMINS)
    def admin_products_menu(m):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🚫 إيقاف منتج", "✅ تشغيل منتج")
        kb.row("⬅️ رجوع")
        bot.send_message(m.chat.id, "اختر إجراء:", reply_markup=kb)

    @bot.message_handler(func=lambda m: m.text in ["🚫 إيقاف منتج", "✅ تشغيل منتج"] and m.from_user.id in ADMINS)
    def toggle_product_prompt(m):
        bot.send_message(m.chat.id, "أدخل رقم معرف المنتج (ID):")
        bot.register_next_step_handler(m, lambda msg: toggle_product_apply(msg, enable=(m.text=="✅ تشغيل منتج")))

    def toggle_product_apply(msg, enable: bool):
        try:
            pid = int(msg.text.strip())
        except Exception:
            return bot.reply_to(msg, "رقم غير صحيح.")
        ok = set_product_active(pid, active=enable)
        if ok:
            log_action(msg.from_user.id, f"{'enable' if enable else 'disable'}_product", f"id={pid}")
            bot.reply_to(msg, ("✅ تم تشغيل المنتج" if enable else "🚫 تم إيقاف المنتج"))
        else:
            bot.reply_to(msg, "لم يتم العثور على المنتج.")

    @bot.message_handler(func=lambda m: m.text == "📊 تقارير سريعة" and m.from_user.id in ADMINS)
    def quick_reports(m):
        dep, pur, top = totals_deposits_and_purchases_syp()
        lines = [f"💰 إجمالي الإيداعات: {dep:,} ل.س", f"🧾 إجمالي الشراء: {pur:,} ل.س"]
        if top:
            lines.append("🏆 الأكثر شراءً:")
            for name, cnt in top:
                lines.append(f"  • {name} — {cnt} عملية")
        bot.send_message(m.chat.id, "\n".join(lines))

    @bot.message_handler(func=lambda m: m.text == "⏳ طابور الانتظار" and m.from_user.id in ADMINS)
    def pending_count(m):
        c = pending_queue_count()
        bot.send_message(m.chat.id, f"عدد الطلبات قيد الانتظار: {c}")

    @bot.message_handler(func=lambda m: m.text == "⚙️ النظام" and m.from_user.id in ADMINS)
    def system_menu(m):
        state = "تشغيل" if not is_maintenance() else "إيقاف (صيانة)"
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🛑 تفعيل وضع الصيانة", "▶️ إلغاء وضع الصيانة")
        kb.row("🔁 إعادة التحقق من الاشتراك الآن")
        kb.row("👥 صلاحيات الأدمن", "📜 Snapshot السجلات")
        kb.row("⬅️ رجوع")
        bot.send_message(m.chat.id, f"حالة النظام: {state}", reply_markup=kb)

    @bot.message_handler(func=lambda m: m.text == "🛑 تفعيل وضع الصيانة" and m.from_user.id in ADMINS)
    def enable_maint(m):
        set_maintenance(True, "🛠️ نعمل على صيانة سريعة الآن. جرّب لاحقًا.")
        log_action(m.from_user.id, "maintenance_on", "")
        bot.reply_to(m, "تم تفعيل وضع الصيانة.")

    @bot.message_handler(func=lambda m: m.text == "▶️ إلغاء وضع الصيانة" and m.from_user.id in ADMINS)
    def disable_maint(m):
        set_maintenance(False)
        log_action(m.from_user.id, "maintenance_off", "")
        bot.reply_to(m, "تم إلغاء وضع الصيانة.")

    @bot.message_handler(func=lambda m: m.text == "🔁 إعادة التحقق من الاشتراك الآن" and m.from_user.id in ADMINS)
    def force_sub(m):
        epoch = force_sub_recheck()
        log_action(m.from_user.id, "force_sub_recheck", str(epoch))
        bot.reply_to(m, "تم مسح الكاش، سيُعاد التحقق للمستخدمين الجدد.")

    @bot.message_handler(func=lambda m: m.text == "📜 Snapshot السجلات" and m.from_user.id in ADMINS)
    def show_logs_snapshot(m):
        tail = get_logs_tail(30)
        if len(tail) > 3500:
            tail = tail[-3500:]
        bot.send_message(m.chat.id, "آخر السجلات:\n" + "```\n" + tail + "\n```", parse_mode="Markdown")

    @bot.message_handler(func=lambda m: m.text == "👥 صلاحيات الأدمن" and m.from_user.id in ADMINS)
    def admins_roles(m):
        # عرض فقط (بدون تعديل بيئي). للإضافة/الإزالة اليدوية لاحقًا.
        from config import ADMINS, ADMIN_MAIN_ID
        ids = ", ".join(str(x) for x in ADMINS)
        bot.send_message(m.chat.id, f"الأدمن الرئيسي: {ADMIN_MAIN_ID}\nالأدمنون: {ids}")
