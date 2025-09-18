# -*- coding: utf-8 -*-
try:
    from validators import parse_amount
except Exception:
    try:
        from services.validators import parse_amount
    except Exception:
        from handlers.validators import parse_amount

# handlers/internet_providers.py — قوائم ADSL مع حجز ذري + رسائل موحّدة

import logging
import re
from telebot import types

from services.wallet_service import (
    register_user_if_not_exist,
    get_balance,
    get_available_balance,   # ✅ المتاح = balance - held
    create_hold,             # ✅ إنشاء الحجز الذرّي
)
from services.discount_service import apply_discount_stacked as apply_discount
from services.referral_service import revalidate_user_discount
from services.offer_badge import badge as offer_badge
# طابور/رسائل للأدمن (اختياري)
try:
    from services.queue_service import add_pending_request, process_queue
except Exception:
    def add_pending_request(*args, **kwargs):
        return None
    def process_queue(*args, **kwargs):
        return None

from services.telegram_safety import remove_inline_keyboard
from services.anti_spam import too_soon

# حارس التأكيد الموحّد (يحذف الكيبورد + Debounce)
try:
    from services.ui_guards import confirm_guard
except Exception:
    from ui_guards import confirm_guard

# (اختياري) حارس الصيانة/الإتاحة + مفاتيح تعطيل عامة
try:
    from services.system_service import is_maintenance, maintenance_message
except Exception:
    def is_maintenance(): return False
    def maintenance_message(): return "🔧 النظام تحت الصيانة مؤقتًا. جرّب لاحقًا."

# حارس الـ Feature Flags (عام/فردي)
try:
    # flag: "internet_adsl" أو "internet" … إلخ
    from services.feature_flags import block_if_disabled
except Exception:
    def block_if_disabled(bot, chat_id, flag_key, nice_name):
        return False

# (اختياري) فحص تفعيل ميزة لإظهار "(موقوف 🔒)" على الأزرار بدون إخفاء
try:
    from services.feature_flags import is_feature_active as _feat_on
except Exception:
    try:
        from services.feature_flags import is_active as _feat_on
    except Exception:
        def _feat_on(key: str, default: bool = True) -> bool:
            return default

# (اختياري) فتح قائمة الشحن عند الحاجة
try:
    from handlers import keyboards
except Exception:
    keyboards = None

# =====================================
#       إعدادات عامة / ثوابت
# =====================================
BAND = "━━━━━━━━━━━━━━━━"
COMMISSION_PER_10000 = 1400
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."

# ✅ قائمة المزودات
INTERNET_PROVIDERS = [
    "هايبر نت", "أم تي أن", "تكامل", "آية", "أمواج", "دنيا", "ليزر",
    "رن نت", "آينت", "زاد", "لاين نت", "برو نت", "أمنية",
    "MTS", "سوا", "يارا",
    # الإضافات
    "مزود بطاقات", "الجمعية SCS", "فيو", "سما نت", "هايفي", "السورية للاتصالات",
]

INTERNET_SPEEDS = [
    {"label": "512 كيلو",  "price": 14500},
    {"label": "1 ميغا",    "price": 19000},
    {"label": "2 ميغا",    "price": 24500},
    {"label": "4 ميغا",    "price": 38500},
    {"label": "8 ميغا",    "price": 64500},
    {"label": "16 ميغا",   "price": 83500},
]

# 🔑 مفاتيح Feature لكل مزوّد (للمنع بدون إخفاء الزر)
PROVIDER_KEYS = {
    "هايبر نت": "internet_provider_hypernet",
    "أم تي أن": "internet_provider_mtn",
    "تكامل": "internet_provider_takamol",
    "آية": "internet_provider_aya",
    "أمواج": "internet_provider_amwaj",
    "دنيا": "internet_provider_dunia",
    "ليزر": "internet_provider_laser",
    "رن نت": "internet_provider_rannet",
    "آينت": "internet_provider_aint",
    "زاد": "internet_provider_zad",
    "لاين نت": "internet_provider_linenet",
    "برو نت": "internet_provider_pronet",
    "أمنية": "internet_provider_omnia",
    "MTS": "internet_provider_mts",
    "سوا": "internet_provider_sawa",
    "يارا": "internet_provider_yara",
    "مزود بطاقات": "internet_provider_cards",
    "الجمعية SCS": "internet_provider_scs",
    "فيو": "internet_provider_view",
    "سما نت": "internet_provider_samanet",
    "هايفي": "internet_provider_haifi",
    "السورية للاتصالات": "internet_provider_syrian_telecom",
}

def _prov_flag_key(name: str):
    return PROVIDER_KEYS.get(name)

# حالة المستخدم (نوع الطلب والخطوات)
user_net_state = {}  # { user_id: { step, provider?, speed?, price?, phone?, price_before?, discount? } }

# =====================================
#   أدوات مساعدة / تنسيق موحّد
# =====================================
_PHONE_RE = re.compile(r"[+\d]+")

def _name(bot, uid) -> str:
    try:
        ch = bot.get_chat(uid)
        nm = (getattr(ch, "first_name", None) or getattr(ch, "full_name", "") or "").strip()
        return nm or "صاحبنا"
    except Exception:
        return "صاحبنا"

def _normalize_phone(txt: str) -> str:
    if not txt:
        return ""
    clean = txt.replace(" ", "").replace("-", "").replace("_", "")
    m = _PHONE_RE.findall(clean)
    return "".join(m)

def _fmt_syp(n) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _commission(amount: int) -> int:
    if amount <= 0:
        return 0
    # سقف لأعلى (كل 10000 عليها 1400): بدون أعداد عشرية
    blocks = (amount + 10000 - 1) // 10000
    return blocks * COMMISSION_PER_10000

def _client_card(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def _with_cancel(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _admin_card(lines: list[str]) -> str:
    return "\n".join(lines)

def _service_unavailable_guard(bot, chat_id) -> bool:
    """يرجع True إذا الخدمة غير متاحة (صيانة/Flag)."""
    if is_maintenance():
        bot.send_message(chat_id, maintenance_message())
        return True
    if block_if_disabled(bot, chat_id, "internet_adsl", "دفع مزودات الإنترنت"):
        return True
    if block_if_disabled(bot, chat_id, "internet", "دفع مزودات الإنترنت"):
        return True
    return False

# =====================================
#   مفاتيح callback
# =====================================
CB_PROV_PREFIX   = "iprov"         # اختيار مزوّد
CB_SPEED_PREFIX  = "ispeed"        # اختيار سرعة
CB_BACK_PROV     = "iback_prov"    # رجوع لقائمة المزودين
CB_BACK_SPEED    = "iback_speed"   # رجوع لقائمة السرعات
CB_CONFIRM       = "iconfirm"      # تأكيد الطلب
CB_CANCEL        = "icancel"       # إلغاء
CB_RECHARGE      = "irecharge"     # شحن المحفظة (اختياري)

# =====================================
#   لوحات أزرار Inline
# =====================================
def _provider_inline_kb() -> types.InlineKeyboardMarkup:
    """الزر يبقى ظاهر دائمًا؛ نضيف وسم (موقوف 🔒) إن كان المزوّد متوقفًا."""
    kb = types.InlineKeyboardMarkup(row_width=3)
    btns = []
    for name in INTERNET_PROVIDERS:
        key = _prov_flag_key(name)
        disabled = (key is not None and not _feat_on(key, True))
        label = f"🌐 {name}" + (" (موقوف 🔒)" if disabled else "")
        btns.append(types.InlineKeyboardButton(label, callback_data=f"{CB_PROV_PREFIX}:{name}"))
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL))
    return kb

def _speeds_inline_kb(user_id: int | None = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = []
    for idx, speed in enumerate(INTERNET_SPEEDS):
        label = f"{speed['label']} • {_fmt_syp(speed['price'])}"
        if user_id:
            label = offer_badge(label, user_id, with_percent=False)
        btns.append(types.InlineKeyboardButton(label, callback_data=f"{CB_SPEED_PREFIX}:{idx}"))
    kb.add(*btns)
    kb.add(types.InlineKeyboardButton("⬅️ رجوع للمزوّدين", callback_data=CB_BACK_PROV))
    return kb

def _confirm_inline_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ تأكيد", callback_data=CB_CONFIRM),
        types.InlineKeyboardButton("⬅️ رجوع للسرعات", callback_data=CB_BACK_SPEED),
        types.InlineKeyboardButton("❌ إلغاء", callback_data=CB_CANCEL),
    )
    return kb

def _insufficient_kb() -> types.InlineKeyboardMarkup | None:
    kb = types.InlineKeyboardMarkup()
    if keyboards and hasattr(keyboards, "recharge_menu"):
        kb.add(types.InlineKeyboardButton("💳 شحن المحفظة", callback_data=CB_RECHARGE))
        kb.add(types.InlineKeyboardButton("⬅️ رجوع للسرعات", callback_data=CB_BACK_SPEED))
        return kb
    return None

# =====================================
#   التسجيل
# =====================================
def register(bot):
    # /cancel — إلغاء سريع من أي خطوة
    @bot.message_handler(commands=['cancel'])
    def _cancel_all(msg):
        uid = msg.from_user.id
        user_net_state.pop(uid, None)
        txt = _client_card("✅ تم الإلغاء", [f"يا {_name(bot, uid)}، رجعناك لقائمة المزودين."])
        bot.send_message(msg.chat.id, _with_cancel(txt), reply_markup=_provider_inline_kb())

    # فتح القائمة الرئيسية (زر ريبلاي)
    @bot.message_handler(func=lambda msg: msg.text == "🌐 دفع مزودات الإنترنت ADSL")
    def open_net_menu(msg):
        try:
            from handlers.start import _reset_user_flows
            _reset_user_flows(msg.from_user.id)
        except Exception:
            pass
        if too_soon(msg.from_user.id, "internet_open", 1.2):
            return
        if _service_unavailable_guard(bot, msg.chat.id):
            return
        register_user_if_not_exist(msg.from_user.id, msg.from_user.full_name)
        start_internet_provider_menu(bot, msg)

    # أوامر مختصرة
    @bot.message_handler(commands=['internet', 'adsl'])
    def cmd_internet(msg):
        if too_soon(msg.from_user.id, "internet_open", 1.2):
            return
        if _service_unavailable_guard(bot, msg.chat.id):
            return
        register_user_if_not_exist(msg.from_user.id, msg.from_user.full_name)
        start_internet_provider_menu(bot, msg)

    # اختيار مزوّد
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_PROV_PREFIX}:"))
    def cb_choose_provider(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        uid = call.from_user.id
        nm = _name(bot, uid)
        provider = call.data.split(":", 1)[1]
        if provider not in INTERNET_PROVIDERS:
            return bot.answer_callback_query(call.id, "❌ خيار غير صالح.", show_alert=True)

        # 🔒 منع الدخول لهذا المزوّد لو متوقّف (بدون إخفاء الزر)
        k = _prov_flag_key(provider)
        if k and block_if_disabled(bot, call.message.chat.id, k, f"مزود — {provider}"):
            try:
                bot.answer_callback_query(call.id, "🚫 هذا المزوّد موقوف مؤقتًا.", show_alert=True)
            except Exception:
                pass
            return

        user_net_state[uid] = {"step": "choose_speed", "provider": provider}
        txt_raw = _client_card(
            f"⚡ يا {nm}، اختار السرعة المطلوبة",
            [f"💸 العمولة لكل 10000 ل.س: {_fmt_syp(COMMISSION_PER_10000)}"]
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=_with_cancel(txt_raw),
            reply_markup=_speeds_inline_kb(user_id=call.from_user.id)
        )
        bot.answer_callback_query(call.id)

    # رجوع لقائمة المزوّدين
    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_PROV)
    def cb_back_to_prov(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        uid = call.from_user.id
        nm = _name(bot, uid)
        user_net_state[uid] = {"step": "choose_provider"}
        txt_raw = _client_card(
            f"⚠️ يا {nm}، اختار مزوّد الإنترنت",
            [f"💸 العمولة لكل 10000 ل.س: {_fmt_syp(COMMISSION_PER_10000)}"]
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=_with_cancel(txt_raw),
            reply_markup=_provider_inline_kb()
        )
        bot.answer_callback_query(call.id)

    # اختيار سرعة
    @bot.callback_query_handler(func=lambda c: c.data.startswith(f"{CB_SPEED_PREFIX}:"))
    def cb_choose_speed(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        uid = call.from_user.id
        nm = _name(bot, uid)
        try:
            idx = int(call.data.split(":", 1)[1])
            speed = INTERNET_SPEEDS[idx]
        except (ValueError, IndexError):
            return bot.answer_callback_query(call.id, "❌ خيار غير صالح.", show_alert=True)

        st = user_net_state.setdefault(uid, {})
        st.update({
            "step": "enter_phone",
            "provider": st.get("provider"),
            "speed": speed["label"],
            "price": speed["price"]
        })
        bot.answer_callback_query(call.id)
        txt_raw = _client_card(
            f"📱 يا {nm}، ابعت رقم الهاتف/الحساب المطلوب شحنه",
            ["يُفضّل مع رمز المحافظة (مثال: 011XXXXXXX)"]
        )
        bot.send_message(call.message.chat.id, _with_cancel(txt_raw))

    # رجوع لشاشة السرعات
    @bot.callback_query_handler(func=lambda c: c.data == CB_BACK_SPEED)
    def cb_back_to_speed(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)

        uid = call.from_user.id
        nm = _name(bot, uid)
        st = user_net_state.get(uid, {})
        if "provider" not in st:
            return cb_back_to_prov(call)

        st["step"] = "choose_speed"
        txt_raw = _client_card(
            f"⚡ يا {nm}، اختار السرعة المطلوبة",
            [f"💸 العمولة لكل 10000 ل.س: {_fmt_syp(COMMISSION_PER_10000)}"]
        )

        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=_with_cancel(txt_raw),
                reply_markup=_speeds_inline_kb(user_id=call.from_user.id)
            )
        except Exception:
            bot.send_message(
                call.message.chat.id,
                _with_cancel(txt_raw),
                reply_markup=_speeds_inline_kb(user_id=call.from_user.id)
            )

        bot.answer_callback_query(call.id)


    # إلغاء من المستخدم (زر)
    @bot.callback_query_handler(func=lambda c: c.data == CB_CANCEL)
    def cb_cancel(call):
        uid = call.from_user.id
        nm = _name(bot, uid)
        user_net_state.pop(uid, None)
        try:
            remove_inline_keyboard(bot, call.message)
        except Exception:
            pass
        txt = _client_card("✅ تم الإلغاء", [f"يا {nm}، اكتب /start للرجوع للقائمة الرئيسية."])
        bot.send_message(call.message.chat.id, txt)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    # إدخال رقم الهاتف — (هنا نعرض قبل/خصم/بعد مثل المنتجات)
    @bot.message_handler(func=lambda m: user_net_state.get(m.from_user.id, {}).get("step") == "enter_phone")
    def handle_phone_entry(msg):
        uid = msg.from_user.id
        nm = _name(bot, uid)
        phone = _normalize_phone(msg.text)
        if not phone or len(phone) < 5:
            return bot.reply_to(msg, _with_cancel(_client_card("⚠️ الرقم مش واضح", [f"يا {nm}، ابعته تاني بشكل صحيح."])))

        st = user_net_state[uid]
        st["phone"] = phone
        st["step"] = "confirm"

        # ✅ نفس منطق المنتجات: إعادة التحقق + تطبيق الخصم للعرض الأولي
        price_before = int(st["price"])
        try:
            revalidate_user_discount(bot, uid)
        except Exception:
            pass
        price_after, applied_disc = apply_discount(uid, price_before)

        # خزّن (للاستخدام في التأكيد النهائي/الرسائل)
        st["price_before"] = price_before
        if applied_disc:
            st["discount"] = {
                "id":      applied_disc.get("id"),
                "percent": applied_disc.get("percent"),
                "before":  price_before,
                "after":   int(price_after),
            }
        else:
            st["discount"] = None

        # نحسب العمولة والإجمالي على السعر بعد الخصم (أنصف للعميل)
        price = int(price_after)
        comm  = _commission(price)
        total = price + comm

        lines = [
            f"🌐 المزوّد: {st['provider']}",
            f"⚡ السرعة: {st['speed']}",
            *( [f"💰 السعر: {_fmt_syp(price)}"] if not st.get("discount") else [
                f"💰 السعر قبل الخصم: {_fmt_syp(price_before)}",
                f"٪ الخصم: {int(st['discount']['percent'] or 0)}٪",
                f"💰 السعر بعد الخصم: {_fmt_syp(price)}",
            ] ),
            f"🧾 العمولة: {_fmt_syp(comm)}",
            f"✅ الإجمالي: {_fmt_syp(total)}",
            "",
            f"📞 الرقم/الحساب: {phone}",
            "",
            "لو تمام، اضغط (✅ تأكيد) عشان نبعت الطلب للإدارة."
        ]
        bot.send_message(msg.chat.id, _with_cancel(_client_card(f"📦 تفاصيل الطلب — يا {nm}", lines)), reply_markup=_confirm_inline_kb())

    # تأكيد وإرسال إلى طابور الأدمن + إنشاء HOLD
    @bot.callback_query_handler(func=lambda c: c.data == CB_CONFIRM)
    def cb_confirm(call):
        if _service_unavailable_guard(bot, call.message.chat.id):
            return bot.answer_callback_query(call.id)
        uid = call.from_user.id
        nm = _name(bot, uid)

        # ✅ عند التأكيد — احذف الكيبورد فقط + Debounce
        if confirm_guard(bot, call, "internet_confirm"):
            return

        st = user_net_state.get(uid)
        if not st or st.get("step") != "confirm":
            return bot.answer_callback_query(call.id, "انتهت صلاحية هذا الطلب.", show_alert=True)

        # ✅ إعادة التحقق + تطبيق الخصم نهائيًا (قد تتغيّر النسبة)
        price_before = int(st.get("price_before") or st["price"])
        try:
            revalidate_user_discount(bot, uid)
        except Exception:
            pass
        price_after, applied_disc = apply_discount(uid, price_before)

        price = int(price_after)
        comm  = _commission(price)
        total = price + comm

        # خزّن معلومات الخصم النهائية
        if applied_disc:
            st["discount"] = {
                "id":      applied_disc.get("id"),
                "percent": applied_disc.get("percent"),
                "before":  price_before,
                "after":   price,
            }
        else:
            st["discount"] = None

        # ✅ نعتمد على الرصيد المتاح فقط (balance − held)
        available = get_available_balance(uid)
        if available < total:
            missing = total - available
            msg_txt = _client_card(
                "❌ رصيدك مش مكفّي",
                [f"المتاح الحالي: {_fmt_syp(available)}",
                 f"المطلوب: {_fmt_syp(total)}",
                 f"الناقص: {_fmt_syp(missing)}",
                 "اشحن محفظتك وجرب تاني 😉"]
            )
            kb = _insufficient_kb()
            if kb:
                bot.send_message(call.message.chat.id, _with_cancel(msg_txt), reply_markup=kb)
            else:
                bot.send_message(call.message.chat.id, _with_cancel(msg_txt))
            return

        # ✅ إنشاء حجز ذري بدل الخصم الفوري
        hold_id = None
        try:
            reason = f"حجز إنترنت — {st['provider']} {st['speed']}"
            res = create_hold(uid, total, reason)
            d = getattr(res, "data", None)
            if isinstance(d, dict):
                hold_id = d.get("id") or d.get("hold_id")
            elif isinstance(d, (list, tuple)) and d:
                hold_id = d[0].get("id") if (d and isinstance(d[0], dict)) else (d[0] if d else None)
            elif isinstance(d, (int, str)):
                hold_id = d
        except Exception as e:
            logging.exception(f"[INET][{uid}] create_hold failed: {e}")

        if not hold_id:
            bot.send_message(call.message.chat.id, _with_cancel("⚠️ حصلت مشكلة بسيطة وإحنا بنثبت قيمة العملية. جرّب تاني بعد شوية."))
            return

        # رسالة للإدارة (موحّدة)
        balance_now = get_balance(uid)
        admin_text = _admin_card([
            "🌐 طلب دفع إنترنت",
            f"👤 الاسم: {call.from_user.full_name}",
            f"يوزر: @{call.from_user.username or ''}",
            f"آيدي: {uid}",
            f"🏷️ المزود: {st['provider']}",
            f"⚡ السرعة: {st['speed']}",
            f"📞 الرقم/الحساب: {st['phone']}",
            *( [f"💰 السعر: {price:,} ل.س"] if not st.get("discount") else [
                f"💰 السعر قبل الخصم: {int(st['discount']['before']):,} ل.س",
                f"٪ الخصم: {int(st['discount']['percent'] or 0)}٪",
                f"💰 السعر بعد الخصم: {price:,} ل.س",
            ] ),
            f"🧾 العمولة: {comm:,} ل.س",
            f"✅ الإجمالي (محجوز): {total:,} ل.س",
            f"💼 رصيد المستخدم الآن: {balance_now:,} ل.س",
            f"HOLD: {hold_id}"
        ])

        add_pending_request(
            user_id=uid,
            username=call.from_user.username,
            request_text=admin_text,
            payload={
                "type": "internet",
                "provider": st["provider"],
                "speed": st["speed"],
                "phone": st["phone"],
                "price": price,               # بعد الخصم
                "comm": comm,
                "total": total,
                "reserved": total,
                "hold_id": hold_id,           # ✅ مفتاح النجاح في الأدمن
                "price_before": int(st.get("price_before") or price),  # للرجوع عند الحاجة
                "discount": (st.get("discount") or None),
            }
        )
        process_queue(bot)

        # تأكيد للعميل (موحّد)
        ok_txt = _client_card(
            f"✅ تمام يا {nm} — طلبك في السكة 🚀",
            ["بعتنا الطلب للإدارة، التنفيذ عادةً من 1 إلى 4 دقايق (وغالبًا أسرع 😉).",
             "تقدر تبعت طلبات تانية في نفس الوقت — إحنا بنحجز من المتاح بس."]
        )
        bot.send_message(call.message.chat.id, _with_cancel(ok_txt))
        st["step"] = "wait_admin"

    # زر شحن المحفظة (اختياري)
    @bot.callback_query_handler(func=lambda c: c.data == CB_RECHARGE)
    def cb_recharge(call):
        if keyboards and hasattr(keyboards, "recharge_menu"):
            bot.send_message(call.message.chat.id, "💳 اختار طريقة شحن محفظتك:", reply_markup=keyboards.recharge_menu())
        else:
            bot.send_message(call.message.chat.id, "💳 لتعبئة المحفظة: تواصل مع الإدارة أو استخدم قائمة الشحن.")
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

# شاشة بدء المزودين
def start_internet_provider_menu(bot, message):
    if _service_unavailable_guard(bot, message.chat.id):
        return
    nm = _name(bot, message.from_user.id)
    txt_raw = _client_card(
        f"🌐 يا {nm}، اختار مزوّد الإنترنت",
        [f"💸 العمولة لكل 10000 ل.س: {_fmt_syp(COMMISSION_PER_10000)}"]
    )
    bot.send_message(message.chat.id, _with_cancel(txt_raw), reply_markup=_provider_inline_kb())
    user_net_state[message.from_user.id] = {"step": "choose_provider"}
