# -*- coding: utf-8 -*-
# handlers/admin.py

import re
import logging
from datetime import datetime, timedelta
from telebot import types

# التحكم في حذف رسالة الأدمن عند أي إجراء على الطابور
DELETE_ADMIN_MESSAGE_ON_ACTION = False
import threading

from services.ads_service import add_channel_ad
from config import ADMINS, ADMIN_MAIN_ID
from database.db import get_table
from services.state_service import purge_state
from services.products_admin import set_product_active, get_product_active, bulk_ensure_products
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
    # ✅ جداول متخصصة
    add_bill_or_units_purchase,
    add_internet_purchase,
    add_cash_transfer_purchase,
    add_companies_transfer_purchase,
    add_university_fees_purchase,
    add_ads_purchase,
    # ✅ الحجز/التصفية الآمنة
    capture_hold,
    release_hold,
    get_product_by_id,
    add_game_purchase,
)
from services.cleanup_service import delete_inactive_users
from handlers import cash_transfer, companies_transfer

# لقراءة المجموعات/الملفات والمنتجات المعروضة للمستخدمين
from handlers.products import PRODUCTS

# لوحة المزايا (المحفظة وطرق الشحن…)
from services.feature_flags import ensure_seed, list_features, set_feature_active

# محاولة استيراد منظّم الشحن لإزالة القفل المحلي بعد القبول/الإلغاء (استيراد كسول وآمن)
try:
    from handlers import recharge as recharge_handlers
except Exception:
    recharge_handlers = None

# ─────────────────────────────────────
#   حالة داخلية
# ─────────────────────────────────────
_cancel_pending = {}
_accept_pending = {}
_msg_pending = {}

# ─────────────────────────────────────
#   تنسيقات ونصوص
# ─────────────────────────────────────
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT_ADMIN = "✋ اكتب /cancel لإلغاء الوضع الحالي."

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

def _safe(v, dash="—"):
    v = ("" if v is None else str(v)).strip()
    return v if v else dash

# ====== Helpers for extracting number / ID / code safely ======
def _pick_first(*vals):
    for v in vals:
        if v is None:
            continue
        s = (str(v).strip() if not isinstance(v, str) else v.strip())
        if s:
            return s
    return None

_DEFAULT_KEYS = [
    "number","beneficiary_number","msisdn","phone","player_id","account","account_id",
    "target_id","username","user","id","code","serial","voucher","to","to_user"
]

def _extract_identifier(payload: dict, request_text: str = "", prefer_keys=None) -> str:
    keys = list(prefer_keys or []) + _DEFAULT_KEYS
    for k in keys:
        if k in payload:
            v = payload.get(k)
            s = ("" if v is None else str(v)).strip()
            if s:
                return s
    rt = request_text or ""
    patterns = [
        r"الرقم[^:]*:\s*<code>([^<]+)</code>",
        r"الكود[^:]*:\s*<code>([^<]+)</code>",
        r"آيدي[^:]*:\s*<code>([^<]+)</code>",
        r"ID[^:]*:\s*<code>([^<]+)</code>",
        r"player[^:]*:\s*<code>([^<]+)</code>",
        r"account[^:]*:\s*<code>([^<]+)</code>",
    ]
    for pat in patterns:
        m = re.search(pat, rt, flags=re.IGNORECASE)
        if m:
            s = m.group(1).strip()
            if s:
                return s
    return ""

def _amount_from_payload(payload: dict) -> int:
    for k in ("reserved", "total", "price", "amount"):
        v = payload.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0

def _insert_purchase_row(user_id: int, product_id, product_name: str, price: int, player_id: str):
    data = {
        "user_id": user_id,
        "product_id": (int(product_id) if product_id else None),
        "product_name": product_name,
        "price": int(price),
        "player_id": _safe(player_id, dash=""),
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": (datetime.utcnow() + timedelta(hours=15)).isoformat(),
    }
    try:
        get_table("purchases").insert(data).execute()
    except Exception as e:
        logging.exception("insert purchases failed: %s", e)

def _prompt_admin_note(bot, admin_id: int, user_id: int):
    """يطلب من الأدمن كتابة ملاحظة تُرسل للعميل (اختياري)."""
    try:
        _accept_pending[admin_id] = user_id
        bot.send_message(
            admin_id,
            f"✍️ اكتب ملاحظة للعميل الآن (نص أو صورة)، أو اكتب /skip للتخطي.\n{CANCEL_HINT_ADMIN}",
        )
    except Exception:
        pass

# NEW: تنظيف قفل الشحن المحلي بعد إنهاء الطلب من طرف الأدمن
def _clear_recharge_local_lock_safe(user_id: int):
    try:
        if recharge_handlers and hasattr(recharge_handlers, "clear_pending_request"):
            recharge_handlers.clear_pending_request(user_id)
    except Exception as e:
        logging.exception("[ADMIN] clear recharge local lock failed: %s", e)

# ─────────────────────────────────────
#   متصفح المنتجات للأدمن (حسب الملفات)
# ─────────────────────────────────────
def _slug(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-')[:30]

def _admin_products_groups_markup():
    kb = types.InlineKeyboardMarkup()
    for group in PRODUCTS.keys():
        kb.add(types.InlineKeyboardButton(text=f"📁 {group}", callback_data=f"adm_prod_g:{_slug(group)}"))
    return kb

def _admin_products_list_markup(group_name: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in PRODUCTS[group_name]:
        active = get_product_active(p.product_id)
        state = "🟢 شغّال" if active else "🔴 موقوف"
        kb.add(types.InlineKeyboardButton(
            text=f"{state} — {p.name} (#{p.product_id})",
            callback_data=f"adm_prod_i:{p.product_id}"
        ))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع للملفات", callback_data="adm_prod_back"))
    return kb

def _admin_product_actions_markup(pid: int):
    active = get_product_active(pid)
    kb = types.InlineKeyboardMarkup()
    if active:
        kb.add(types.InlineKeyboardButton("🚫 إيقاف المنتج", callback_data=f"adm_prod_t:{pid}:0"))
    else:
        kb.add(types.InlineKeyboardButton("✅ تشغيل المنتج", callback_data=f"adm_prod_t:{pid}:1"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="adm_prod_back"))
    return kb

# ─────────────────────────────────────
#   لوحة المزايا (Feature Flags)
# ─────────────────────────────────────
def _features_markup():
    items = list_features()
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not items:
        kb.add(types.InlineKeyboardButton("لا توجد مزايا مُسجّلة", callback_data="noop"))
        return kb
    for it in items:
        k, label = it.get("key"), it.get("label")
        active = bool(it.get("active", True))
        lamp = "🟢" if active else "🔴"
        to = 0 if active else 1
        kb.add(
            types.InlineKeyboardButton(
                text=f"{lamp} {label}",
                callback_data=f"adm_feat_t:{k}:{to}"
            )
        )
    return kb

# ─────────────────────────────────────
#   التسجيل
# ─────────────────────────────────────
def register(bot, history):
    # تسجيل هاندلرات التحويلات (كما هي)
    cash_transfer.register(bot, history)
    companies_transfer.register_companies_transfer(bot, history)

    # زرع مزايا افتراضية (مرة عند الإقلاع)
    try:
        ensure_seed()
    except Exception:
        pass

    # إلغاء لأي وضع إدخال للأدمن (/cancel)
    @bot.message_handler(commands=['cancel'])
    def _admin_cancel_any(msg: types.Message):
        _msg_pending.pop(msg.from_user.id, None)
        _accept_pending.pop(msg.from_user.id, None)
        bot.reply_to(msg, "✅ تم الإلغاء.")

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
    #  ✉️ رسالة/🖼️ صورة للعميل (HTML + ترويسة بسيطة)
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
        bot.send_message(c.from_user.id, f"📝 اكتب رسالتك بصيغة HTML.\n{CANCEL_HINT_ADMIN}")

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
        bot.send_message(c.from_user.id, f"📷 أرسل الصورة الآن (مع كابتشن HTML إن حبيت).\n{CANCEL_HINT_ADMIN}")

    @bot.message_handler(func=lambda m: m.from_user.id in _msg_pending,
                         content_types=["text", "photo"])
    def forward_to_client(m: types.Message):
        data = _msg_pending.pop(m.from_user.id)
        uid  = data["user_id"]
        if data["mode"] == "text":
            if m.content_type != "text":
                return bot.reply_to(m, "❌ المطلوب نص فقط.")
            bot.send_message(uid, f"{BAND}\n📩 <b>رسالة من الإدارة</b>\n{m.text}\n{BAND}", parse_mode="HTML")
        else:
            if m.content_type != "photo":
                return bot.reply_to(m, "❌ المطلوب صورة فقط.")
            cap = m.caption or ""
            bot.send_photo(uid, m.photo[-1].file_id, caption=f"{BAND}\n📩 <b>رسالة من الإدارة</b>\n{cap}\n{BAND}", parse_mode="HTML")
        bot.reply_to(m, "✅ أُرسلت للعميل. تقدر تكمل بتأكيد/إلغاء الطلب.")

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
        req_text = req.get("request_text") or ""
        name     = _user_name(bot, user_id)

        if DELETE_ADMIN_MESSAGE_ON_ACTION:
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

        # === تأجيل الطلب ===
        if action == "postpone":
            if not (call.from_user.id == ADMIN_MAIN_ID or call.from_user.id in ADMINS or allowed(call.from_user.id, "queue:postpone")):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            # إزالة الكيبورد لتجنُّب النقر المزدوج
            try:
                from services.telegram_safety import remove_inline_keyboard
            except Exception:
                from telegram_safety import remove_inline_keyboard
            try:
                remove_inline_keyboard(bot, call.message)
            except Exception:
                pass
            # تأجيل الطلب بإرجاعه لآخر الدور
            postpone_request(request_id)
            # إبلاغ العميل برسالة اعتذار/تنظيم الدور
            try:
                bot.send_message(
                    user_id,
                    f"⏳ عزيزي {name}، تم تنظيم دور طلبك مجددًا بسبب ضغط أو عُطل مؤقت. "
                    "نعتذر عن التأخير، وسيتم تنفيذ طلبك قريبًا بإذن الله. شكرًا لتفهّمك."
                )
            except Exception as e:
                logging.error(f"[admin] postpone notify error: {e}", exc_info=True)
            # تأكيد للأدمن + بدء فترة الخمول
            try:
                bot.answer_callback_query(call.id, "✅ تم تأجيل الطلب.")
            except Exception:
                pass
            queue_cooldown_start(bot)
            # Safety: schedule explicit re-kick after 31s
            try:
                threading.Timer(31, lambda: process_queue(bot)).start()
            except Exception as e:
                logging.error('[admin] safety timer error: %s', e)
            return
        # === إلغاء الطلب ===
        if action == "cancel":
            if not allowed(call.from_user.id, "queue:cancel"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            hold_id  = payload.get("hold_id")
            reserved = int(payload.get("reserved", 0) or 0)
            typ      = (payload.get("type") or "").strip()

            if hold_id:
                try:
                    r = release_hold(hold_id)
                    if getattr(r, "error", None):
                        logging.error("release_hold error: %s", r.error)
                except Exception as e:
                    logging.exception("release_hold exception: %s", e)
            else:
                if reserved > 0:
                    add_balance(user_id, reserved, "إلغاء حجز (قديم)")

            delete_pending_request(request_id)
            if reserved > 0:
                bot.send_message(user_id, f"🚫 تم إلغاء طلبك.\n🔁 رجّعنا {_fmt_syp(reserved)} من المبلغ المحجوز لمحفظتك — كله تمام 😎")
            else:
                bot.send_message(user_id, "🚫 تم إلغاء طلبك.\n🔁 رجّعنا المبلغ المحجوز (إن وُجد) لمحفظتك.")
            bot.answer_callback_query(call.id, "✅ تم إلغاء الطلب.")
            queue_cooldown_start(bot)

            # NEW: لو طلب شحن — نظّف قفل الشحن المحلي
            if typ in ("recharge", "wallet_recharge", "deposit"):
                _clear_recharge_local_lock_safe(user_id)

            _prompt_admin_note(bot, call.from_user.id, user_id)
            return

        # === قبول الطلب ===
        if action == "accept":
            # ✅ فحص صلاحية التأكيد (مهم)
            if not allowed(call.from_user.id, "queue:confirm"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")

            typ      = (payload.get("type") or "").strip()
            hold_id  = payload.get("hold_id")
            amt      = _amount_from_payload(payload)

            if hold_id:
                try:
                    r = capture_hold(hold_id)
                    if getattr(r, "error", None) or not bool(getattr(r, "data", True)):
                        logging.error("capture_hold failed: %s", getattr(r, "error", r))
                        return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز. أعد المحاولة.")
                except Exception as e:
                    logging.exception("capture_hold exception: %s", e)
                    return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز. أعد المحاولة.")

            # ——— طلبات المنتجات الرقمية ———
            if typ == "order":
                product_id_raw = payload.get("product_id")
                player_id      = _extract_identifier(payload, req_text, ["player_id","account","id","username","user","target_id"])
                amt            = int(amt or payload.get("price", 0) or 0)

                product_name = (payload.get("product_name") or "").strip()
                prod_obj = None
                if not product_name and product_id_raw:
                    try:
                        prod_obj = get_product_by_id(int(product_id_raw))
                    except Exception:
                        prod_obj = None
                    if prod_obj and isinstance(prod_obj, dict):
                        product_name = (prod_obj.get("name") or "").strip()
                if not product_name:
                    product_name = "منتج رقمي"

                pid_for_db = int(product_id_raw) if (product_id_raw and (prod_obj or payload.get("product_name"))) else None

                _insert_purchase_row(user_id, pid_for_db, product_name, amt, _safe(player_id))
                try:
                    add_game_purchase(user_id, pid_for_db, product_name, int(amt), _safe(player_id))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🎉 تمام يا {name}! تم تحويل «{product_name}» لآيدي «{_safe(player_id)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك. استمتع باللعب! 🎮\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            # ——— إعلانات ———
            elif typ in ("ads", "media"):
                amt     = int(amt or payload.get("price", 0) or 0)
                times   = payload.get("count")
                contact = payload.get("contact") or "—"
                ad_text = payload.get("ad_text") or ""
                images  = payload.get("images", [])

                title = f"إعلان مدفوع × {times}" if times else "إعلان مدفوع"
                _insert_purchase_row(user_id, None, title, amt, _safe(contact))
                try:
                    add_ads_purchase(user_id, ad_name=title, price=amt, channel_username=None)
                except Exception:
                    pass

                delete_pending_request(request_id)

                # NEW: أنشئ إعلانًا فعّالًا لبدء النشر الآلي ضمن نافذة 9→22 بتوقيت دمشق
                try:
                    times_total = int(payload.get("times_total") or payload.get("count") or 1)
                    duration_days = int(payload.get("duration_days") or 30)
                    add_channel_ad(
                        user_id=user_id,
                        times_total=times_total,
                        price=amt,
                        contact=contact,
                        ad_text=ad_text,
                        images=images,
                        duration_days=duration_days,
                    )
                except Exception as e:
                    logging.exception("[ADMIN][ADS] add_channel_ad failed: %s", e)

                bot.send_message(
                    user_id,
                    f"{BAND}\n📣 تمام يا {name}! وتم تأكيد باقة الإعلان ({title}). "
                    f"اتخصم {_fmt_syp(amt)} من محفظتك، وحننشرها حسب الجدولة.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            elif typ in ("syr_unit", "mtn_unit"):
                price = int(payload.get("price", 0) or amt or 0)
                num   = _extract_identifier(payload, req_text, ["number","msisdn","phone"])
                unit_name = payload.get("unit_name") or "وحدات"

                _insert_purchase_row(user_id, None, unit_name, price, _safe(num))
                try:
                    add_bill_or_units_purchase(user_id, bill_name=unit_name, price=price, number=_safe(num))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n✅ تمام يا {name}! تم تحويل {unit_name} للرقم «{_safe(num)}» "
                    f"وتم خصم {_fmt_syp(price)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ in ("syr_bill", "mtn_bill"):
                amt   = int(amt or payload.get("price", 0) or 0)
                num   = payload.get("number")
                label = payload.get("unit_name", "فاتورة")

                _insert_purchase_row(user_id, None, label, amt, _safe(num))
                try:
                    add_bill_or_units_purchase(user_id, bill_name=label, price=amt, number=_safe(num))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🧾 تمام يا {name}! تم دفع {label} للرقم «{_safe(num)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ == "internet":
                amt      = int(amt or payload.get("price", 0) or 0)
                provider = _safe(payload.get("provider"), dash="").strip()
                speed    = _safe(payload.get("speed"), dash="").strip()
                phone    = payload.get("phone")
                name_lbl = ("إنترنت " + " ".join(x for x in [provider, speed] if x)).strip() or "إنترنت"

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(phone))
                try:
                    add_internet_purchase(user_id, provider_name=provider or None, price=amt, phone=_safe(phone), speed=speed or None)
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🌐 تمام يا {name}! تم دفع فاتورة الإنترنت ({name_lbl}) للرقم «{_safe(phone)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            elif typ == "cash_transfer":
                amt       = int(amt or payload.get("price", 0) or 0)
                number    = payload.get("number")
                cash_type = _safe(payload.get("cash_type"), dash="").strip()
                name_lbl  = (f"تحويل كاش {cash_type}".strip() if cash_type else "تحويل كاش")

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(number))
                try:
                    add_cash_transfer_purchase(user_id, transfer_name=name_lbl, price=amt, number=_safe(number))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n💸 تمام يا {name}! تم تنفيذ {name_lbl} للرقم «{_safe(number)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ == "companies_transfer":
                amt                = int(amt or payload.get("price", 0) or 0)
                company            = _safe(payload.get("company"), dash="").strip()
                beneficiary_number = payload.get("beneficiary_number")
                name_lbl           = (f"حوالة مالية عبر {company}".strip() if company else "حوالة مالية")

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(beneficiary_number))
                try:
                    add_companies_transfer_purchase(user_id, company_name=(company or None), price=amt, beneficiary_number=_safe(beneficiary_number))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🏢 تمام يا {name}! تم تنفيذ {name_lbl} للمستفيد «{_safe(beneficiary_number)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ in ("university_fees",):
                amt           = int(amt or payload.get("price", 0) or 0)
                university    = _safe(payload.get("university"), dash="").strip()
                university_id = payload.get("university_id")
                name_lbl      = (f"رسوم جامعية ({university})".strip() if university else "رسوم جامعية")

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(university_id))
                try:
                    add_university_fees_purchase(user_id, university_name=(university or None), price=amt, university_id=_safe(university_id))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🎓 تمام يا {name}! تم دفع {name_lbl} للرقم الجامعي «{_safe(university_id)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

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

                bot.send_message(user_id, f"{BAND}\n⚡ يا {name}، تم شحن محفظتك بمبلغ {_fmt_syp(amount)} بنجاح. دوس واشتري اللي نفسك فيه! 😉\n{BAND}")
                bot.answer_callback_query(call.id, "✅ تم تنفيذ عملية الشحن")
                queue_cooldown_start(bot)

                # NEW: نظّف قفل الشحن المحلي بعد القبول
                _clear_recharge_local_lock_safe(user_id)

                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            else:
                return bot.answer_callback_query(call.id, "❌ نوع الطلب غير معروف.")

        bot.answer_callback_query(call.id, "❌ حدث خطأ غير متوقع.")

    # === ملاحظة الإدمن بعد القبول/الإلغاء (اختياري) ===
    @bot.message_handler(func=lambda m: m.from_user.id in _accept_pending,
                         content_types=["text", "photo"])
    def handle_accept_message(msg: types.Message):
        user_id = _accept_pending.get(msg.from_user.id)
        if not user_id:
            return
        if msg.text and msg.text.strip() == "/skip":
            bot.send_message(msg.chat.id, "✅ تم التخطي.")
        elif msg.content_type == "text":
            bot.send_message(user_id, f"{BAND}\n📝 <b>ملاحظة من الإدارة</b>\n{msg.text.strip()}\n{BAND}", parse_mode="HTML")
            bot.send_message(msg.chat.id, "✅ أُرسلت الملاحظة للعميل.")
        elif msg.content_type == "photo":
            bot.send_photo(user_id, msg.photo[-1].file_id, caption=f"{BAND}\n📝 <b>ملاحظة من الإدارة</b>\n{BAND}", parse_mode="HTML")
            bot.send_message(msg.chat.id, "✅ أُرسلت الصورة للعميل.")
        else:
            bot.send_message(msg.chat.id, "❌ نوع الرسالة غير مدعوم. ابعت نص أو صورة، أو /skip للتخطي.")
        _accept_pending.pop(msg.from_user.id, None)

    # ===== قائمة الأدمن =====
    @bot.message_handler(commands=['admin'])
    def admin_menu(msg):
        if msg.from_user.id not in ADMINS:
            return bot.reply_to(msg, "صلاحية الأدمن فقط.")
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🛒 إدارة المنتجات", "🧩 تشغيل/إيقاف المزايا")
        kb.row("📊 تقارير سريعة", "⏳ طابور الانتظار")
        kb.row("⚙️ النظام", "⬅️ رجوع")
        bot.send_message(msg.chat.id, "لوحة الأدمن:", reply_markup=kb)

    @bot.message_handler(func=lambda m: m.text == "🛒 إدارة المنتجات" and m.from_user.id in ADMINS)
    def admin_products_menu(m):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🚫 إيقاف منتج", "✅ تشغيل منتج")
        kb.row("🔄 مزامنة المنتجات (DB)")
        kb.row("⬅️ رجوع")
        bot.send_message(m.chat.id, "اختر إجراء:", reply_markup=kb)

    # ✅ بدّل إدخال الـID بمتصفح ملفات/منتجات إنلاين
    @bot.message_handler(func=lambda m: m.text in ["🚫 إيقاف منتج", "✅ تشغيل منتج"] and m.from_user.id in ADMINS)
    def admin_products_browser(m):
        bot.send_message(m.chat.id, "اختر الملف لعرض منتجاته:", reply_markup=_admin_products_groups_markup())

    # 🔄 مزامنة كل المنتجات المعرفة في PRODUCTS إلى جدول products
    @bot.message_handler(func=lambda m: m.text == "🔄 مزامنة المنتجات (DB)" and m.from_user.id in ADMINS)
    def seed_products(m):
        try:
            items = []
            for group, arr in PRODUCTS.items():
                for p in arr:
                    items.append((p.product_id, p.name, group))
            created = bulk_ensure_products(items)
            bot.reply_to(m, f"✅ تمت المزامنة.\nأُنشئ/تأكّد {created} صف(ًا).")
        except Exception as e:
            logging.exception("[ADMIN] bulk ensure products failed: %s", e)
            bot.reply_to(m, "❌ فشلت المزامنة. تفقد السجلات.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_prod_g:") and c.from_user.id in ADMINS)
    def adm_group_open(call: types.CallbackQuery):
        slug = call.data.split(":", 1)[1]
        group_name = next((g for g in PRODUCTS.keys() if _slug(g) == slug), None)
        if not group_name:
            return bot.answer_callback_query(call.id, "❌ المجموعة غير موجودة.")
        try:
            bot.edit_message_text(f"📁 {group_name} — اختر منتجًا:", call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_products_list_markup(group_name))
        except Exception:
            # لو تعذّر التعديل أرسل رسالة جديدة
            bot.send_message(call.message.chat.id, f"📁 {group_name} — اختر منتجًا:", reply_markup=_admin_products_list_markup(group_name))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == "adm_prod_back" and c.from_user.id in ADMINS)
    def adm_back(call: types.CallbackQuery):
        try:
            bot.edit_message_text("اختر الملف لعرض منتجاته:", call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_products_groups_markup())
        except Exception:
            bot.send_message(call.message.chat.id, "اختر الملف لعرض منتجاته:", reply_markup=_admin_products_groups_markup())
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_prod_i:") and c.from_user.id in ADMINS)
    def adm_product_open(call: types.CallbackQuery):
        pid = int(call.data.split(":", 1)[1])
        state = "شغّال 🟢" if get_product_active(pid) else "موقوف 🔴"
        txt = f"المنتج #{pid}\nالحالة الحالية: {state}\nيمكنك تبديل الحالة:"
        try:
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_product_actions_markup(pid))
        except Exception:
            bot.send_message(call.message.chat.id, txt, reply_markup=_admin_product_actions_markup(pid))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_prod_t:") and c.from_user.id in ADMINS)
    def adm_product_toggle(call: types.CallbackQuery):
        # كان سابقًا: _, pid, to = call.data.split(":")
        try:
            _, pid, to = call.data.split(":", 2)  # آمن حتى لو زاد المحتوى مستقبلًا
        except ValueError:
            return bot.answer_callback_query(call.id, "❌ تنسيق غير صحيح.")
        pid, to = int(pid), bool(int(to))
        ok = set_product_active(pid, to)
        if not ok:
            return bot.answer_callback_query(call.id, "❌ تعذّر تحديث الحالة.")
        log_action(call.from_user.id, f"{'enable' if to else 'disable'}_product", f"id={pid}")
        state = "✅ تم تشغيل المنتج" if to else "🚫 تم إيقاف المنتج"
        txt = f"المنتج #{pid}\n{state}\nالحالة الآن: {'شغّال 🟢' if to else 'موقوف 🔴'}"
        try:
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_product_actions_markup(pid))
        except Exception:
            bot.send_message(call.message.chat.id, txt, reply_markup=_admin_product_actions_markup(pid))
        bot.answer_callback_query(call.id, "تم التحديث.")

    # ===== لوحة المزايا (Feature Flags) =====
    @bot.message_handler(func=lambda m: m.text == "🧩 تشغيل/إيقاف المزايا" and m.from_user.id in ADMINS)
    def features_menu(m):
        bot.send_message(m.chat.id, "بدّل حالة المزايا التالية:", reply_markup=_features_markup())

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_feat_t:") and c.from_user.id in ADMINS)
    def adm_feature_toggle(call: types.CallbackQuery):
        # كان سابقًا: _, key, to = call.data.split(":")
        try:
            _, rest = call.data.split(":", 1)   # "adm_feat_t:<KEY>:<TO>"  => rest="<KEY>:<TO>"
            key, to = rest.rsplit(":", 1)       # يسمح بوجود ":" داخل <KEY>
        except ValueError:
            return bot.answer_callback_query(call.id, "❌ تنسيق غير صحيح.")
        ok = set_feature_active(key, bool(int(to)))
        if not ok:
            return bot.answer_callback_query(call.id, "❌ تعذّر تحديث الميزة.")
        # تحديث اللوحة الحالية
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=_features_markup())
        except Exception:
            pass
        bot.answer_callback_query(call.id, "✅ تم التحديث.")

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
