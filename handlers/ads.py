from telebot import types
from services.wallet_service import get_balance, deduct_balance
from services.queue_service import add_pending_request, process_queue

# ----------------------------------
# خيارات الإعلان
# ----------------------------------
AD_OPTIONS = [
    ("✨ إعلان مرة (5000 ل.س)", 1, 5000),
    ("🔥 إعلان مرتين (15000 ل.س)", 2, 15000),
    ("🌟 إعلان 3 مرات (25000 ل.س)", 3, 25000),
    ("🚀 إعلان 4 مرات (40000 ل.س)", 4, 40000),
    ("💎 إعلان 5 مرات (60000 ل.س)", 5, 60000),
    ("🏆 إعلان 10 مرات (100000 ل.س)", 10, 100000),
]

user_ads_state: dict[int, dict] = {}

# ====================================================================
# التسجيل
# ====================================================================

def register(bot, _history):
    """تسجيل جميع هاندلرات مسار الإعلانات."""

    # ----------------------------------------------------------------
    # 1) فتح قائمة الإعلانات
    # ----------------------------------------------------------------
    @bot.message_handler(func=lambda msg: msg.text == "📢 إعلاناتك")
    def open_ads_menu(msg):
        markup = types.InlineKeyboardMarkup()
        for text, times, _ in AD_OPTIONS:
            markup.add(types.InlineKeyboardButton(text, callback_data=f"ads_{times}"))
        markup.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ads_back"))
        bot.send_message(msg.chat.id, "🟢 اختر نوع إعلانك:", reply_markup=markup)

    # ----------------------------------------------------------------
    # 2) اختيار نوع الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ads_") and call.data[4:].isdigit())
    def select_ad_type(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        times = int(call.data.split("_")[1])

        for _text, t, price in AD_OPTIONS:
            if t == times:
                user_ads_state[user_id] = {
                    "times": times,
                    "price": price,
                    "step": "contact",
                }
                break

        bot.send_message(call.message.chat.id, "✏️ أرسل رقم التواصل، صفحتك أو موقعك (سيظهر للإعلان):")

    # ----------------------------------------------------------------
    # 3) استقبال وسيلة التواصل
    # ----------------------------------------------------------------
    @bot.message_handler(content_types=["text"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "contact")
    def receive_contact(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["contact"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "confirm_contact"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("تأكيد", callback_data="ads_contact_confirm"), types.InlineKeyboardButton("إلغاء", callback_data="ads_cancel"))
        bot.send_message(msg.chat.id, f"📞 سيتم عرض للتواصل:\n{msg.text}\n\nهل تريد المتابعة؟", reply_markup=markup)

    # ----------------------------------------------------------------
    # 4) تأكيد وسيلة التواصل أو إلغاء
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in {"ads_contact_confirm", "ads_cancel"})
    def confirm_contact(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        if call.data == "ads_contact_confirm":
            user_ads_state[user_id]["step"] = "ad_text"
            bot.send_message(call.message.chat.id, "📝 أرسل نص إعلانك (سيظهر في القناة):")
        else:
            user_ads_state.pop(user_id, None)
            bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    # ----------------------------------------------------------------
    # 5) استقبال نص الإعلان
    # ----------------------------------------------------------------
    @bot.message_handler(content_types=["text"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "ad_text")
    def receive_ad_text(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["ad_text"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "wait_image_option"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📸 أضف صورة واحدة", callback_data="ads_one_image"), types.InlineKeyboardButton("🖼️ أضف صورتين", callback_data="ads_two_images"), types.InlineKeyboardButton("➡️ تخطي الصور", callback_data="ads_skip_images"))
        bot.send_message(msg.chat.id, "🖼️ يمكنك اختيار إضافة صورة واحدة أو صورتين أو تخطي:", reply_markup=markup)

    # ----------------------------------------------------------------
    # 6) تحديد عدد الصور المطلوب
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in {"ads_one_image", "ads_two_images"})
    def choose_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        expect = 1 if call.data == "ads_one_image" else 2
        state = user_ads_state.setdefault(user_id, {})
        state.update({"expect_images": expect, "images": [], "step": "wait_images"})
        bot.send_message(call.message.chat.id, "📸 أرسل الصورة الآن." if expect == 1 else "📸 أرسل الصورتين الآن واحدة تلو الأخرى.")

    # ----------------------------------------------------------------
    # 7) استقبال الصور
    # ----------------------------------------------------------------
    @bot.message_handler(content_types=["photo", "document"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "wait_images")
    def receive_images(msg):
        user_id = msg.from_user.id
        state = user_ads_state.get(user_id)
        if not state:
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

        state.setdefault("images", []).append(file_id)

        if len(state["images"]) >= state["expect_images"]:
            state["step"] = "confirm"
            preview_ad(msg.chat.id, user_id)
        else:
            remaining = state["expect_images"] - len(state["images"])
            bot.send_message(msg.chat.id, f"📸 أرسل الصورة المتبقية ({remaining} متبقية).")

    # ----------------------------------------------------------------
    # 8) تخطي الصور
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_skip_images")
    def skip_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        state = user_ads_state.get(user_id, {})
        state["step"] = "confirm"
        preview_ad(call.message.chat.id, user_id)

    # ----------------------------------------------------------------
    # 9) معاينة الإعلان
    # ----------------------------------------------------------------
    def preview_ad(chat_id: int, user_id: int):
        data = user_ads_state.get(user_id)
        if not data:
            bot.send_message(chat_id, "⚠️ انتهت جلسة الإعلان. ابدأ من جديد.")
            return

        imgs = data.get("images", [])
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
            f"{data['ad_text']}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📱 للتواصل:\n"
            f"{data['contact']}\n"
            "━━━━━━━━━━━━━━━━━━"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ تأكيد الإعلان", callback_data="ads_confirm_send"), types.InlineKeyboardButton("📝 تعديل الإعلان", callback_data="ads_edit"), types.InlineKeyboardButton("❌ إلغاء", callback_data="ads_cancel"))
        bot.send_message(chat_id, ad_preview, reply_markup=markup, parse_mode="HTML")

    # ----------------------------------------------------------------
    # 10) تعديل الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_edit")
    def edit_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_ads_state[user_id]["step"] = "ad_text"
        bot.send_message(call.message.chat.id, "🔄 عدل نص إعلانك أو أرسل إعلان جديد:")

    # ----------------------------------------------------------------
    # 11) إلغاء الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_cancel")
    def cancel_ad(call):
        bot.answer
