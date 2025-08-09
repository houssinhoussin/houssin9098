# handlers/ads.py

from telebot import types
from services.wallet_service import get_balance, deduct_balance
from services.queue_service import add_pending_request, process_queue
from handlers.keyboards import main_menu

# ⇦ الجديد: استخدام تخزين الحالة في Supabase بدل القاموس المحلي
from services.state_service import get_state, set_state, delete_state

# ----------------------------------
# خيارات الإعلان (كما هي)
# ----------------------------------
AD_OPTIONS = [
    ("✨ إعلان مرة (5000 ل.س)", 1, 5000),
    ("🔥 إعلان مرتين (15000 ل.س)", 2, 15000),
    ("🌟 إعلان 3 مرات (25000 ل.س)", 3, 25000),
    ("🚀 إعلان 4 مرات (40000 ل.س)", 4, 40000),
    ("💎 إعلان 5 مرات (60000 ل.س)", 5, 60000),
    ("🏆 إعلان 10 مرات (100000 ل.س)", 10, 100000),
]

# مفتاح حالة تدفّق الإعلان في جدول user_state
ADS_KEY = "ads_flow"
# مدة صلاحية جلسة الإعلان (ثواني) — ساعة
ADS_TTL = 3600

def _get_ads_state(user_id: int) -> dict:
    """إرجاع حالة الإعلان للمستخدم (dict) أو {}."""
    return get_state(user_id, ADS_KEY) or {}

def _set_ads_state(user_id: int, data: dict):
    """حفظ حالة الإعلان مع TTL."""
    set_state(user_id, ADS_KEY, data, ttl_seconds=ADS_TTL)

def _clear_ads_state(user_id: int):
    """حذف حالة الإعلان (إنهاء الجلسة)."""
    delete_state(user_id, ADS_KEY)

# ====================================================================
# التسجيل
# ====================================================================

def register(bot, _history):
    """تسجيل جميع هاندلرات مسار الإعلانات."""

    # ----------------------------------------------------------------
    # 1) مدخل الإعلان – رسالة ترويجية أولية (كما هي)
    # ----------------------------------------------------------------
    @bot.message_handler(func=lambda msg: msg.text == "📢 إعلاناتك")
    def ads_entry(msg):
        promo = (
            "✨ <b>مساحة إعلانات متجرنا</b> ✨\n\n"
            "عبر قناتنا <a href=\"https://t.me/shop100sho\">@shop100sho</a> تصل رسالتك إلى <b>آلاف</b> المشتركين يوميًا!\n"
            "• روِّج منتجك أو أعرض أسعارك الجديدة\n"
            "• ابحث عن سلعة أو عقار\n"
            "• أعلن عن عقار أو سيارة للبيع\n"
            "• انشر فرصة عمل أو ابحث عن وظيفة\n\n"
            "🚀 اضغط «زيارة القناة» للاطّلاع، ثم «متابعة» للبدء الآن."
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔎 زيارة القناة", url="https://t.me/shop100sho"))
        markup.add(types.InlineKeyboardButton("✅ متابعة", callback_data="ads_start"))
        bot.send_message(msg.chat.id, promo, reply_markup=markup, parse_mode="HTML")

    # ----------------------------------------------------------------
    # 1-bis) متابعة إلى باقات الإعلان (كما هي)
    # ----------------------------------------------------------------
    def send_ads_menu(chat_id):
        mk = types.InlineKeyboardMarkup()
        for text, times, _ in AD_OPTIONS:
            mk.add(types.InlineKeyboardButton(text, callback_data=f"ads_{times}"))
        mk.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ads_back"))
        bot.send_message(chat_id, "🟢 اختر نوع إعلانك:", reply_markup=mk)

    @bot.callback_query_handler(func=lambda call: call.data == "ads_start")
    def proceed_to_ads(call):
        bot.answer_callback_query(call.id)
        send_ads_menu(call.message.chat.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ads_back")
    def ads_back(call):
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "عدنا إلى القائمة الرئيسية.",
            reply_markup=main_menu()
        )

    # ----------------------------------------------------------------
    # 2) اختيار نوع الإعلان (كما هي)
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ads_") and call.data[4:].isdigit())
    def select_ad_type(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        times = int(call.data.split("_")[1])

        # العثور على الباقة المختارة
        selected = None
        for _text, t, price in AD_OPTIONS:
            if t == times:
                selected = {"times": t, "price": price, "step": "contact"}
                break

        if not selected:
            return bot.send_message(call.message.chat.id, "❌ خيار غير صالح، أعد المحاولة.")

        _set_ads_state(user_id, selected)

        bot.send_message(
            call.message.chat.id,
            "✏️ أرسل رقم التواصل، صفحتك أو موقعك (سيظهر للإعلان):"
        )

    # ----------------------------------------------------------------
    # 3) استقبال وسيلة التواصل
    # ----------------------------------------------------------------
    @bot.message_handler(
        content_types=["text"],
        func=lambda msg: _get_ads_state(msg.from_user.id).get("step") == "contact"
    )
    def receive_contact(msg):
        user_id = msg.from_user.id
        st = _get_ads_state(user_id)
        if not st:
            return

        st["contact"] = msg.text.strip()
        st["step"] = "confirm_contact"
        _set_ads_state(user_id, st)

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("تأكيد", callback_data="ads_contact_confirm"),
            types.InlineKeyboardButton("إلغاء", callback_data="ads_cancel")
        )
        bot.send_message(
            msg.chat.id,
            f"📞 سيتم عرض للتواصل:\n{msg.text}\n\nهل تريد المتابعة؟",
            reply_markup=markup
        )

    # ----------------------------------------------------------------
    # 4) تأكيد وسيلة التواصل أو إلغاء
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in {"ads_contact_confirm", "ads_cancel"})
    def confirm_contact(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        st = _get_ads_state(user_id)

        if call.data == "ads_contact_confirm":
            if not st:
                return bot.send_message(call.message.chat.id, "⚠️ انتهت الجلسة. ابدأ من جديد.")
            st["step"] = "ad_text"
            _set_ads_state(user_id, st)
            bot.send_message(call.message.chat.id, "📝 أرسل نص إعلانك (سيظهر في القناة):")
        else:
            _clear_ads_state(user_id)
            bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    # ----------------------------------------------------------------
    # 5) استقبال نص الإعلان
    # ----------------------------------------------------------------
    @bot.message_handler(
        content_types=["text"],
        func=lambda msg: _get_ads_state(msg.from_user.id).get("step") == "ad_text"
    )
    def receive_ad_text(msg):
        user_id = msg.from_user.id
        st = _get_ads_state(user_id)
        if not st:
            return

        st["ad_text"] = msg.text.strip()
        st["step"] = "wait_image_option"
        _set_ads_state(user_id, st)

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("📸 أضف صورة واحدة", callback_data="ads_one_image"),
            types.InlineKeyboardButton("🖼️ أضف صورتين", callback_data="ads_two_images"),
            types.InlineKeyboardButton("➡️ تخطي الصور", callback_data="ads_skip_images"),
        )
        bot.send_message(msg.chat.id, "🖼️ يمكنك اختيار إضافة صورة واحدة أو صورتين أو تخطي:", reply_markup=markup)

    # ----------------------------------------------------------------
    # 6) تحديد عدد الصور المطلوب
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in {"ads_one_image", "ads_two_images"})
    def choose_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        st = _get_ads_state(user_id) or {}
        expect = 1 if call.data == "ads_one_image" else 2
        st.update({"expect_images": expect, "images": [], "step": "wait_images"})
        _set_ads_state(user_id, st)
        bot.send_message(
            call.message.chat.id,
            "📸 أرسل الصورة الآن." if expect == 1 else "📸 أرسل الصورتين الآن واحدة تلو الأخرى."
        )

    # ----------------------------------------------------------------
    # 7) استقبال الصور
    # ----------------------------------------------------------------
    @bot.message_handler(
        content_types=["photo", "document"],
        func=lambda msg: _get_ads_state(msg.from_user.id).get("step") == "wait_images"
    )
    def receive_images(msg):
        user_id = msg.from_user.id
        st = _get_ads_state(user_id)
        if not st:
            return

        file_id = None
        if msg.content_type == "photo":
            file_id = msg.photo[-1].file_id
        elif msg.content_type == "document":
            mime = getattr(msg.document, "mime_type", "")
            if mime.startswith("image/"):
                file_id = msg.document.file_id

        if not file_id:
            bot.send_message(msg.chat.id, "❌ الملف المرسل ليس صورة صالحة.")
            return

        imgs = st.setdefault("images", [])
        imgs.append(file_id)
        _set_ads_state(user_id, st)

        if len(imgs) >= st.get("expect_images", 0):
            st["step"] = "confirm"
            _set_ads_state(user_id, st)
            preview_ad(msg.chat.id, user_id)
        else:
            remaining = st["expect_images"] - len(imgs)
            bot.send_message(msg.chat.id, f"📸 أرسل الصورة المتبقية ({remaining} متبقية).")

    # ----------------------------------------------------------------
    # 8) تخطي الصور
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_skip_images")
    def skip_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        st = _get_ads_state(user_id) or {}
        st["step"] = "confirm"
        _set_ads_state(user_id, st)
        preview_ad(call.message.chat.id, user_id)

    # ----------------------------------------------------------------
    # 9) معاينة الإعلان
    # ----------------------------------------------------------------
    def preview_ad(chat_id: int, user_id: int):
        st = _get_ads_state(user_id)
        if not st:
            bot.send_message(chat_id, "⚠️ انتهت جلسة الإعلان. ابدأ من جديد.")
            return

        imgs = st.get("images", [])
        if imgs:
            try:
                if len(imgs) == 1:
                    bot.send_photo(chat_id, imgs[0])
                else:
                    media = [types.InputMediaPhoto(fid) for fid in imgs]
                    bot.send_media_group(chat_id, media)
            except Exception:
                bot.send_message(chat_id, "⚠️ تعذر عرض الصور، سيتم المتابعة بدونها.")

        ad_preview = (
            "<b><u>📢 إعـــــــلان</u></b>\n\n"
            f"{st.get('ad_text', '')}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📱 للتواصل:\n"
            f"{st.get('contact', '')}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد الإعلان", callback_data="ads_confirm_send"),
            types.InlineKeyboardButton("📝 تعديل الإعلان", callback_data="ads_edit"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="ads_cancel"),
        )
        bot.send_message(chat_id, ad_preview, reply_markup=markup, parse_mode="HTML")

    # ----------------------------------------------------------------
    # 10) تعديل الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_edit")
    def edit_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        st = _get_ads_state(user_id) or {}
        st["step"] = "ad_text"
        _set_ads_state(user_id, st)
        bot.send_message(call.message.chat.id, "🔄 عدل نص إعلانك أو أرسل إعلان جديد:")

    # ----------------------------------------------------------------
    # 11) إلغاء الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_cancel")
    def cancel_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        _clear_ads_state(user_id)
        bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    # ----------------------------------------------------------------
    # 12) تأكيد الإعلان (إرساله للطابور)
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_confirm_send")
    def confirm_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        st = _get_ads_state(user_id)

        # التحقق من المرحلة
        if not st or st.get("step") != "confirm":
            bot.send_message(call.message.chat.id, "⚠️ انتهت الجلسة أو حصل خطأ، أعد المحاولة من جديد.")
            _clear_ads_state(user_id)
            return

        price   = st["price"]
        balance = get_balance(user_id)

        # رصيد غير كافٍ
        if balance is None or balance < price:
            missing = price - (balance or 0)
            bot.send_message(
                call.message.chat.id,
                f"❌ رصيدك غير كافٍ لهذا الإعلان.\nالناقص: {missing:,} ل.س"
            )
            return

        # ——— حجز المبلغ (خصم مؤقت) ———
        deduct_balance(user_id, price)           # حجز
        new_balance = get_balance(user_id)       # رصيد بعد الحجز

        # نص يُرسل للمشرفين
        admin_msg = (
            "🆕 طلب إعلان جديد\n"
            f"👤 <code>{call.from_user.full_name}</code>  —  "
            f"@{call.from_user.username or 'بدون يوزر'}\n"
            f"آيدي: <code>{user_id}</code>\n\n"
            f"🔖 عدد التكرار: {st['times']} مرّة\n"
            f"💵 السعر: {price:,} ل.س\n"
            f"💰 الرصيد بعد الحجز: {new_balance:,} ل.س"
        )

        # إنشاء الـ payload (كما هو)
        payload = {
            "type": "ads",
            "count": st["times"],
            "price": price,
            "contact": st["contact"],
            "ad_text": st["ad_text"],
            "images": st.get("images", []),
            "reserved": price        # مبلغ محجوز بانتظار موافقة الإدارة
        }

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload=payload,
        )

        # معالجة فورية إن توفّر
        process_queue(bot)

        bot.send_message(user_id, "✅ تم إرسال إعلانك إلى الإدارة لمراجعته.")
        _clear_ads_state(user_id)
