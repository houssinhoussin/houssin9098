# -*- coding: utf-8 -*-
# handlers/wholesale.py

from telebot import types
from config import ADMIN_MAIN_ID
from services.wallet_service import register_user_if_not_exist
import logging
import re

# طابور الإشعارات
try:
    from services.queue_service import add_pending_request, process_queue
except Exception:
    def add_pending_request(*args, **kwargs): return None
    def process_queue(*args, **kwargs): return None

# أعلام المزايا (إيقاف/تشغيل) — نُبقي الزر ظاهر لكن نوقف الخدمة
try:
    from services.feature_flags import block_if_disabled
except Exception:
    def block_if_disabled(bot, chat_id, flag_key, nice_name): return False

# حرس نقر سريع + تنظيف كيبورد (Inline)
try:
    from services.anti_spam import too_soon
except Exception:
    def too_soon(*a, **k): return False

try:
    from services.telegram_safety import remove_inline_keyboard
except Exception:
    def remove_inline_keyboard(*a, **k): pass

BAND = "━━━━━━━━━━━━━━━━"

# حالة جلسة "الجملة"
user_wholesale_state: dict[int, dict] = {}

WHOLESALE_BUTTON_TEXT = "📦 طلب احتياجات منزلية او تجارية"

WHOLESALE_DESCRIPTION = (
    "🛒 <b>خدمة الطلبات بالجملة</b>\n\n"
    "الخدمة دي مخصوص لأصحاب المحلات والمراكز التجارية:\n"
    "• غذائية: رز، شاي، زيت، سكر، معلبات\n"
    "• مشروبات: غازية، مياه، عصائر\n"
    "• حلويات: شوكولا، بسكويت، سكاكر\n"
    "• منظفات وعناية: مسحوق، صابون، شامبو…\n\n"
    "✍️ اكتب دلوقتي تفاصيل المطلوب (الأنواع + الكميات).\n"
    "📎 تقدر كمان تبعت <b>صورة</b> لقائمة الطلبات/فاتورة وسنحفظها مع طلبك."
)

_phone_re = re.compile(r"[+\d]+")

STEPS = ["products", "address", "phone", "store", "confirm"]
PREV_OF = {
    "products": None,
    "address": "products",
    "phone": "address",
    "store": "phone",
    "confirm": "store",
}

def _name(u):
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صاحبنا"

def _norm_phone(txt: str) -> str:
    if not txt: return ""
    clean = txt.replace(" ", "").replace("-", "").replace("_", "")
    parts = _phone_re.findall(clean)
    return "".join(parts)

def _nav_kb():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add("⬅️ رجوع", "❌ إلغاء")
    return mk

def _remove_kb():
    return types.ReplyKeyboardRemove()

def _ok_send_msg(name: str) -> str:
    return (
        f"{BAND}\n"
        f"✅ تمام يا {name}! طلبك اتبعت للإدارة.\n"
        f"📞 هنتواصل معاك قريب جدًا للتأكيد والتفاصيل.\n"
        f"{BAND}"
    )

def _summary_card(uid: int) -> str:
    d = user_wholesale_state.get(uid, {})
    has_photo = "نعم" if d.get("photo_file_id") else "لا"
    return (
        f"{BAND}\n"
        "🛍️ <b>مراجعة الطلب</b>\n\n"
        f"📦 <b>المطلوب:</b> {d.get('products','—')}\n"
        f"📍 <b>العنوان:</b> {d.get('address','—')}\n"
        f"📞 <b>الهاتف:</b> {d.get('phone','—')}\n"
        f"🏪 <b>اسم المتجر:</b> {d.get('store_name','—')}\n"
        f"📎 <b>صورة مرفقة:</b> {has_photo}\n"
        f"{BAND}\n"
        "لو كل حاجة تمام اضغط «تأكيد الإرسال».\n"
        "اكتب /cancel أو اضغط «❌ إلغاء» للإلغاء."
    )

def _confirm_kb():
    mk = types.InlineKeyboardMarkup()
    mk.add(
        types.InlineKeyboardButton("✅ تأكيد الإرسال", callback_data="ws_confirm"),
        types.InlineKeyboardButton("📝 تعديل", callback_data="ws_edit"),
    )
    mk.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ws_back"))
    mk.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="ws_cancel"))
    return mk

# ==== أسئلة كل خطوة (مع كيبورد الرجوع/الإلغاء) ====

def _ask_products(bot, chat_id):
    bot.send_message(
        chat_id,
        f"{WHOLESALE_DESCRIPTION}\n\n"
        "🖼️ لو عندك صورة لقائمة الطلبات/فاتورة، ابعتها الآن (سنحفظها مع الطلب).\n"
        "تقدر ترجع للخلف بـ «⬅️ رجوع» أو تلغي بـ «❌ إلغاء».",
        parse_mode="HTML",
        reply_markup=_nav_kb()
    )

def _ask_address(bot, chat_id):
    bot.send_message(
        chat_id,
        "📍 اكتب عنوان المتجر أو منطقة التوصيل:",
        reply_markup=_nav_kb()
    )

def _ask_phone(bot, chat_id):
    bot.send_message(
        chat_id,
        "📞 اكتب رقم الموبايل للتواصل:",
        reply_markup=_nav_kb()
    )

def _ask_store(bot, chat_id):
    bot.send_message(
        chat_id,
        "🏪 اكتب اسم المتجر:",
        reply_markup=_nav_kb()
    )

def _show_summary(bot, uid, chat_id):
    bot.send_message(
        chat_id,
        _summary_card(uid),
        parse_mode="HTML",
        reply_markup=_confirm_kb()
    )

def _goto_step(bot, uid, step, chat_id):
    """انتقل إلى خطوة محددة وأرسل سؤالها."""
    user_wholesale_state.setdefault(uid, {})["step"] = step
    if step == "products": _ask_products(bot, chat_id)
    elif step == "address": _ask_address(bot, chat_id)
    elif step == "phone": _ask_phone(bot, chat_id)
    elif step == "store": _ask_store(bot, chat_id)
    elif step == "confirm": _show_summary(bot, uid, chat_id)

def _reset_session(uid):
    user_wholesale_state.pop(uid, None)

# ================== التسجيل ==================

def register(bot, user_state):

    # /cancel — يلغي الجلسة من أي خطوة
    @bot.message_handler(commands=["cancel"], func=lambda m: False)
    def ws_cancel_cmd(msg):
        uid = msg.from_user.id
        if uid in user_wholesale_state:
            _reset_session(uid)
            bot.reply_to(msg, "✅ اتلغت العملية. تقدر تبدأ من جديد وقت ما تحب.", reply_markup=_remove_kb())
        else:
            bot.reply_to(msg, "لا يوجد إجراء جارٍ حاليًا.", reply_markup=_remove_kb())

    # بدء خدمة الجملة — الزر يبقى ظاهر لكن الخدمة تتوقف عند التعطيل
    @bot.message_handler(func=lambda msg: msg.text == WHOLESALE_BUTTON_TEXT)
    def start_wholesale(msg):
        if block_if_disabled(bot, msg.chat.id, "wholesale", "شراء جملة"):
            # الدالة عندك عادةً تُرسل رسالة «الميزة موقوفة» وتمنع التنفيذ
            return

        # إنهاء أي رحلة/مسار سابق عالق (اختياري)
        try:
            from handlers.start import _reset_user_flows
            _reset_user_flows(msg.from_user.id)
        except Exception:
            pass

        uid = msg.from_user.id
        try:
            register_user_if_not_exist(uid, msg.from_user.full_name)
        except Exception:
            pass

        # تهيئة حالة الجلسة
        user_wholesale_state[uid] = {"step": "products"}
        # (اختياري) علامة لمنظومة عامة عندك
        try:
            user_state[uid] = "wholesale"
        except Exception:
            pass

        _ask_products(bot, msg.chat.id)

    # استقبال صورة مرفقة (فاتورة/قائمة) في أي خطوة داخل الجملة
    @bot.message_handler(content_types=['photo'], func=lambda m: m.from_user.id in user_wholesale_state)
    def ws_photo(msg):
        uid = msg.from_user.id
        st = user_wholesale_state.setdefault(uid, {})
        try:
            file_id = msg.photo[-1].file_id if msg.photo else None
        except Exception:
            file_id = None

        if not file_id:
            return bot.reply_to(msg, "⚠️ لم أتمكن من حفظ الصورة. جرّب من جديد.")

        st["photo_file_id"] = file_id

        # إن كانت الخطوة Products وتعليق الصورة يصلح وصفًا، استخدمه
        cap = (msg.caption or "").strip()
        if st.get("step") == "products" and cap and len(cap) >= 4 and not st.get("products"):
            st["products"] = cap
            bot.reply_to(msg, "📎 تم حفظ الصورة واعتمدنا تفاصيل القائمة من التعليق.", reply_markup=_nav_kb())
            return _goto_step(bot, uid, "address", msg.chat.id)

        bot.reply_to(msg, "📎 تم حفظ الصورة مع الطلب. أكمل بقية البيانات.", reply_markup=_nav_kb())

    # زر "❌ إلغاء" كنص — يلغي الجلسة (فقط إن المستخدم داخل الجملة)
    @bot.message_handler(func=lambda msg: msg.text == "❌ إلغاء" and msg.from_user.id in user_wholesale_state)
    def ws_cancel_text(msg):
        uid = msg.from_user.id
        _reset_session(uid)
        bot.send_message(uid, "✅ اتلغت العملية. نورتنا 🙏", reply_markup=_remove_kb())

    # زر "⬅️ رجوع" كنص — يرجع خطوة (فقط إن المستخدم داخل الجملة)
    @bot.message_handler(func=lambda msg: msg.text == "⬅️ رجوع" and msg.from_user.id in user_wholesale_state)
    def ws_back_text(msg):
        uid = msg.from_user.id
        st = user_wholesale_state.get(uid, {})
        cur = st.get("step")
        prev = PREV_OF.get(cur)
        if not prev:
            # لو في أول خطوة، نعيد السؤال ذاته
            return _goto_step(bot, uid, "products", msg.chat.id)
        # انتقل للخطوة السابقة
        _goto_step(bot, uid, prev, msg.chat.id)

    # خطوة: تفاصيل المنتجات (نص)
    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "products", content_types=['text'])
    def get_product_details(msg):
        uid = msg.from_user.id
        text = (msg.text or "").strip()
        # رفض لو ضغط المستخدم "⬅️ رجوع/❌ إلغاء" — معالجينها فوق
        if text in ("⬅️ رجوع", "❌ إلغاء"): return

        # تحقق بسيط: طول مناسب
        if len(text) < 4:
            return bot.reply_to(msg, "⚠️ التفاصيل قليلة جدًا. اكتب الأنواع + الكميات.\nمثال: سكر 10كغ، زيت 5 عبوات.", reply_markup=_nav_kb())

        user_wholesale_state[uid]["products"] = text
        _goto_step(bot, uid, "address", msg.chat.id)

    # خطوة: العنوان
    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "address", content_types=['text'])
    def get_address(msg):
        uid = msg.from_user.id
        text = (msg.text or "").strip()
        if text in ("⬅️ رجوع", "❌ إلغاء"): return
        if len(text) < 3:
            return bot.reply_to(msg, "⚠️ العنوان غير كافٍ. اكتب المنطقة + أقرب نقطة دلالة.", reply_markup=_nav_kb())

        user_wholesale_state[uid]["address"] = text
        _goto_step(bot, uid, "phone", msg.chat.id)

    # خطوة: الهاتف
    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "phone", content_types=['text'])
    def get_phone(msg):
        uid = msg.from_user.id
        text = (msg.text or "").strip()
        if text in ("⬅️ رجوع", "❌ إلغاء"): return

        phone = _norm_phone(text)
        # تحقق صارم قليلًا: 8–15 رقم (يسمح +)
        digits_only = re.sub(r"\D", "", phone)
        if len(digits_only) < 8 or len(digits_only) > 15:
            return bot.reply_to(msg, "⚠️ الرقم غير صالح. اكتب رقم من 8–15 رقم.\nمثال: 09xxxxxxxx", reply_markup=_nav_kb())

        user_wholesale_state[uid]["phone"] = phone
        _goto_step(bot, uid, "store", msg.chat.id)

    # خطوة: اسم المتجر
    @bot.message_handler(func=lambda msg: user_wholesale_state.get(msg.from_user.id, {}).get("step") == "store", content_types=['text'])
    def get_store_name(msg):
        uid = msg.from_user.id
        text = (msg.text or "").strip()
        if text in ("⬅️ رجوع", "❌ إلغاء"): return
        if len(text) < 2:
            return bot.reply_to(msg, "⚠️ اكتب اسم متجر صحيح.", reply_markup=_nav_kb())

        user_wholesale_state[uid]["store_name"] = text
        _goto_step(bot, uid, "confirm", msg.chat.id)

    # أزرار التأكيد/التعديل/الإلغاء/الرجوع (Inline في شاشة المراجعة)
    @bot.callback_query_handler(func=lambda c: c.data in {"ws_confirm", "ws_cancel", "ws_edit", "ws_back"})
    def ws_actions(c: types.CallbackQuery):
        uid = c.from_user.id
        st = user_wholesale_state.get(uid)
        if not st:
            try:
                bot.answer_callback_query(c.id, "انتهت الجلسة. ابدأ من جديد لو سمحت.", show_alert=True)
            except Exception:
                pass
            return

        # تنظيف الكيبورد (ومنع الدبل-كليك)
        try:
            remove_inline_keyboard(bot, c.message)
        except Exception:
            pass
        if too_soon(uid, "ws_actions", seconds=2):
            try:
                return bot.answer_callback_query(c.id, "⏱️ تم الاستلام..")
            except Exception:
                return

        if c.data == "ws_cancel":
            _reset_session(uid)
            try: bot.answer_callback_query(c.id, "تم الإلغاء")
            except Exception: pass
            return bot.send_message(uid, "✅ اتلغت العملية. نورتنا 🙏", reply_markup=_remove_kb())

        if c.data == "ws_back":
            # رجوع خطوة واحدة من شاشة المراجعة → إلى "store"
            st["step"] = "store"
            return _goto_step(bot, uid, "store", c.message.chat.id)

        if c.data == "ws_edit":
            # ارجَع لأول نقطة (المنتجات) لإعادة الإدخال بالكامل
            st["step"] = "products"
            return _goto_step(bot, uid, "products", c.message.chat.id)

        # تأكيد
        if st.get("step") != "confirm":
            try:
                return bot.answer_callback_query(c.id, "الطلب غير جاهز للتأكيد.", show_alert=True)
            except Exception:
                return

        name = _name(c.from_user)
        text = (
            "🛍️ <b>طلب جملة جديد</b>\n\n"
            f"👤 المستخدم: <code>{name}</code> | ID: <code>{uid}</code>\n"
            f"📦 المطلوب: {st.get('products')}\n"
            f"🏪 المتجر: {st.get('store_name')}\n"
            f"📍 العنوان: {st.get('address')}\n"
            f"📞 الهاتف: {st.get('phone')}\n"
        )

        # إرسال للطابور (+ إرفاق الصورة في الـ payload إن وُجدت)
        add_pending_request(
            user_id=uid,
            username=c.from_user.username or "—",
            request_text=text,
            payload={
                "type": "wholesale",
                "products": st.get("products"),
                "store_name": st.get("store_name"),
                "address": st.get("address"),
                "phone": st.get("phone"),
                "photo_file_id": st.get("photo_file_id"),  # جديد
            }
        )
        process_queue(bot)

        # نسخة للأدمن: إن وُجدت صورة نرسلها مع الكابتشن؛ وإلا رسالة نص
        try:
            photo_id = st.get("photo_file_id")
            if photo_id:
                bot.send_photo(ADMIN_MAIN_ID, photo_id, caption=text, parse_mode="HTML")
            else:
                bot.send_message(ADMIN_MAIN_ID, text, parse_mode="HTML")
        except Exception:
            logging.exception("[WHOLESALE] فشل إرسال نسخة للأدمن")

        # رسالة للعميل
        bot.send_message(uid, _ok_send_msg(_name(c.from_user)), parse_mode="HTML", reply_markup=_remove_kb())
        _reset_session(uid)
        try:
            bot.answer_callback_query(c.id, "تم الإرسال ✅")
        except Exception:
            pass
