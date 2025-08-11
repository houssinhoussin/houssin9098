# handlers/admin.py

import re
import logging
from datetime import datetime, timedelta
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
    # ✅ واجهات الحجز/التصفية الآمنة
    capture_hold,
    release_hold,
    get_product_by_id,
    add_game_purchase,
)
from services.cleanup_service import delete_inactive_users
from handlers import cash_transfer, companies_transfer

_cancel_pending = {}
_accept_pending = {}
_msg_pending = {}

# ---------- Helpers موحّدة للرسائل ----------
def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _user_name(bot, user_id: int) -> str:
    try:
        ch = bot.get_chat(user_id)
        name = getattr(ch, "first_name", None) or getattr(ch, "full_name", None) or ""
        name = (name or "").strip()
        return name if name else "صديقنا"
    except Exception:
        return "صديقنا"

def _amount_from_payload(payload: dict) -> int:
    """محاولة ذكية لاستخراج المبلغ من أي مفتاح محتمل داخل الـ payload."""
    for k in ("reserved", "total", "price", "amount"):
        v = payload.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0

def _insert_purchase_row(user_id: int, product_id, product_name: str, price: int, player_id: str):
    """إدراج في purchases بدون خصم (الخصم تم عبر capture_hold)."""
    data = {
        "user_id": user_id,
        "product_id": (int(product_id) if product_id else None),
        "product_name": product_name,
        "price": int(price),
        "player_id": str(player_id or ""),
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": (datetime.utcnow() + timedelta(hours=15)).isoformat(),
    }
    try:
        get_table("purchases").insert(data).execute()
    except Exception as e:
        logging.exception("insert purchases failed: %s", e)

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
        name     = _user_name(bot, user_id)

        # حذف رسالة الأدمن
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        # === تأجيل الطلب ===
        if action == "postpone":
            if not allowed(call.from_user.id, "queue:cancel"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            postpone_request(request_id)
            bot.send_message(user_id, f"⏳ يا {name}، رجّعنا طلبك لآخر الطابور. هنكمله أول ما نيجي عليه.")
            bot.answer_callback_query(call.id, "✅ تم تأجيل الطلب.")
            queue_cooldown_start(bot)
            return

        # === إلغاء الطلب ===
        if action == "cancel":
            if not allowed(call.from_user.id, "queue:cancel"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            hold_id  = payload.get("hold_id")
            reserved = int(payload.get("reserved", 0) or 0)

            if hold_id:
                # إلغاء الحجز الذرّي
                try:
                    r = release_hold(hold_id)
                    if getattr(r, "error", None):
                        logging.error("release_hold error: %s", r.error)
                except Exception as e:
                    logging.exception("release_hold exception: %s", e)
            else:
                # مسار قديم بدون hold_id: نردّ المبلغ يدويًا لو كان تم خصمه سابقًا
                if reserved > 0:
                    add_balance(user_id, reserved, "إلغاء حجز (قديم)")

            delete_pending_request(request_id)
            if reserved > 0:
                bot.send_message(user_id, f"🚫 تم إلغاء طلبك.\n🔁 رجّعنا {_fmt_syp(reserved)} من المبلغ المحجوز لمحفظتك — كله تمام 😎")
            else:
                bot.send_message(user_id, "🚫 تم إلغاء طلبك.\n🔁 رجّعنا المبلغ المحجوز (إن وُجد) لمحفظتك.")
            bot.answer_callback_query(call.id, "✅ تم إلغاء الطلب.")
            queue_cooldown_start(bot)
            return

        # === قبول الطلب ===
        if action == "accept":
            typ      = payload.get("type")
            hold_id  = payload.get("hold_id")
            amt      = _amount_from_payload(payload)

            # لو فيه حجز، صفّيه أولًا (خصم فعلي) — بدون أي خصم إضافي لاحقًا
            if hold_id:
                try:
                    r = capture_hold(hold_id)
                    if getattr(r, "error", None) or not bool(r.data):
                        logging.error("capture_hold failed: %s", getattr(r, "error", r.data))
                        return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز. أعد المحاولة.")
                except Exception as e:
                    logging.exception("capture_hold exception: %s", e)
                    return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز. أعد المحاولة.")

            # ——— طلبات المنتجات الرقمية ———
            if typ == "order":
                product_id_raw = payload.get("product_id")
                player_id      = payload.get("player_id")
                amt            = int(amt or payload.get("price", 0) or 0)

                # جلب اسم المنتج (إن وجد)
                try:
                    prod = get_product_by_id(int(product_id_raw)) if product_id_raw else None
                except Exception:
                    prod = None

                pid_for_db = int(product_id_raw) if (product_id_raw and prod) else None
                product_name = (prod.get("name") if prod else None) or f"طلب منتج #{product_id_raw}"

                # إدراج في purchases بدون خصم (الخصم تم عبر capture_hold)
                _insert_purchase_row(user_id, pid_for_db, product_name, amt, str(player_id))
                # تسجيل في game_purchases (Write-through)
                try:
                    add_game_purchase(user_id, pid_for_db, product_name, int(amt), str(player_id or ""))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"🎉 تمام يا {name}! تم تحويل {product_name} لآيدي «{player_id}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك. استمتع باللعب! 🎮",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— وحدات (سيرياتيل/MTN) ———
            elif typ in ("syr_unit", "mtn_unit"):
                price = int(payload.get("price", 0) or amt or 0)
                num   = payload.get("number")
                unit_name = payload.get("unit_name") or "وحدات"

                _insert_purchase_row(user_id, None, unit_name, price, str(num))
                try:
                    add_bill_or_units_purchase(user_id, bill_name=unit_name, price=price, number=str(num))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"✅ تمام يا {name}! تم تحويل {unit_name} للرقم «{num}» "
                    f"وتم خصم {_fmt_syp(price)} من محفظتك.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— فواتير (سيرياتيل/MTN) ———
            elif typ in ("syr_bill", "mtn_bill"):
                amt   = int(amt or payload.get("price", 0) or 0)
                num   = payload.get("number")
                label = payload.get("unit_name", "فاتورة")

                _insert_purchase_row(user_id, None, label, amt, str(num))
                try:
                    add_bill_or_units_purchase(user_id, bill_name=label, price=amt, number=str(num))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"🧾 تمام يا {name}! تم دفع {label} للرقم «{num}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— إنترنت ———
            elif typ == "internet":
                amt      = int(amt or payload.get("price", 0) or 0)
                provider = payload.get("provider")
                speed    = payload.get("speed")
                phone    = payload.get("phone")
                name_lbl = f"إنترنت {provider} {speed}".strip()

                _insert_purchase_row(user_id, None, name_lbl, amt, str(phone))
                try:
                    add_internet_purchase(user_id, provider_name=provider, price=amt, phone=str(phone), speed=speed)
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"🌐 تمام يا {name}! تم دفع فاتورة الإنترنت ({provider} {speed}) للرقم «{phone}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— تحويل نقدي ———
            elif typ == "cash_transfer":
                amt       = int(amt or payload.get("price", 0) or 0)
                number    = payload.get("number")
                cash_type = payload.get("cash_type")
                name_lbl  = f"تحويل كاش {cash_type}".strip()

                _insert_purchase_row(user_id, None, name_lbl, amt, str(number))
                try:
                    add_cash_transfer_purchase(user_id, transfer_name=name_lbl, price=amt, number=str(number))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"💸 تمام يا {name}! تم تحويل {name_lbl} للرقم «{number}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— تحويلات شركات ———
            elif typ == "companies_transfer":
                amt                = int(amt or payload.get("price", 0) or 0)
                company            = payload.get("company")
                beneficiary_number = payload.get("beneficiary_number")
                name_lbl           = f"حوالة مالية عبر {company}".strip()

                _insert_purchase_row(user_id, None, name_lbl, amt, str(beneficiary_number))
                try:
                    add_companies_transfer_purchase(user_id, company_name=company, price=amt, beneficiary_number=str(beneficiary_number))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"🏢 تمام يا {name}! تم تحويل شركة عبر {company} للمستفيد «{beneficiary_number}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— رسوم جامعية ———
            elif typ == "university_fees":
                amt           = int(amt or payload.get("price", 0) or 0)
                university    = payload.get("university")
                university_id = payload.get("university_id")
                name_lbl      = f"دفع رسوم جامعية ({university})".strip()

                _insert_purchase_row(user_id, None, name_lbl, amt, str(university_id))
                try:
                    add_university_fees_purchase(user_id, university_name=university, price=amt, university_id=str(university_id))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"🎓 تمام يا {name}! تم دفع رسوم جامعية ({university}) للرقم الجامعي «{university_id}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                return

            # ——— شحن محفظة ———
            elif typ in ("recharge", "wallet_recharge", "deposit"):
                amount = _amount_from_payload(payload) or payload.get("amount") or 0
                amount = int(amount) if amount else 0
                if amount <= 0:
                    return bot.answer_callback_query(call.id, "❌ مبلغ الشحن غير صالح.")

                try:
                    logging.info(f"[ADMIN][RECHARGE][{user_id}] approve amount={amount} req_id={request_id}")
                except Exception:
                    pass

                add_balance(user_id, amount, "شحن محفظة — من الإدارة")
                delete_pending_request(request_id)

                bot.send_message(user_id, f"⚡ يا {name}، تم شحن محفظتك بمبلغ {_fmt_syp(amount)} بنجاح. دُوس واشتري اللي نفسك فيه! 😉")
                bot.answer_callback_query(call.id, "✅ تم تنفيذ عملية الشحن")
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

    # ===== تسجيل هاندلرات قائمة الأدمن =====

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
        from config import ADMINS, ADMIN_MAIN_ID
        ids = ", ".join(str(x) for x in ADMINS)
        bot.send_message(m.chat.id, f"الأدمن الرئيسي: {ADMIN_MAIN_ID}\nالأدمنون: {ids}")
