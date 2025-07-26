from telebot import types
from services.wallet_service import get_balance, deduct_balance
from services.queue_service import add_pending_request, process_queue

# خيارات الإعلان
AD_OPTIONS = [
    ("✨ إعلان مرة (5000 ل.س)", 1, 5000),
    ("🔥 إعلان مرتين (15000 ل.س)", 2, 15000),
    ("🌟 إعلان 3 مرات (25000 ل.س)", 3, 25000),
    ("🚀 إعلان 4 مرات (40000 ل.س)", 4, 40000),
    ("💎 إعلان 5 مرات (60000 ل.س)", 5, 60000),
    ("🏆 إعلان 10 مرات (100000 ل.س)", 10, 100000),
]

# حالة جلسات الإعلان لكل مستخدم
user_ads_state = {}

def register(bot, history):
    """يسجل جميع هاندلرات مسار الإعلانات فى البوت."""

    # 1️⃣ الدخول إلى قائمة الإعلانات
    @bot.message_handler(func=lambda msg: msg.text == "📢 إعلاناتك")
    def open_ads_menu(msg):
        markup = types.InlineKeyboardMarkup()
        for text, times, price in AD_OPTIONS:
            markup.add(types.InlineKeyboardButton(text, callback_data=f"ads_{times}"))
        markup.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ads_back"))
        bot.send_message(msg.chat.id, "🟢 اختر نوع إعلانك:", reply_markup=markup)

    # 2️⃣ اختيار نوع الإعلان
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ads_") and call.data[4:].isdigit())
    def select_ad_type(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        times = int(call.data.split("_")[1])

        # تعريف الحالة الأولية
        for text, t, price in AD_OPTIONS:
            if t == times:
                user_ads_state[user_id] = {
                    "times": times,
                    "price": price,
                    "step": "contact",
                }
                break

        bot.send_message(
            call.message.chat.id,
            "✏️ أرسل رقم التواصل، صفحتك أو موقعك (سيظهر للإعلان):"
        )

    # 3️⃣ استقبال وسيلة التواصل
    @bot.message_handler(
        content_types=["text"],
        func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "contact",
    )
    def receive_contact(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["contact"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "ad_text"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("تأكيد", callback_data="ads_contact_confirm"))
        markup.add(types.InlineKeyboardButton("إلغاء", callback_data="ads_cancel"))

        bot.send_message(
            msg.chat.id,
            f"📞 سيتم عرض للتواصل:\n{msg.text}\n\nهل تريد المتابعة؟",
            reply_markup=markup,
        )

    # 4️⃣ تأكيد وسيلة التواصل أو إلغاء
    @bot.callback_query_handler(func=lambda call: call.data in ["ads_contact_confirm", "ads_cancel"])
    def confirm_contact(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id

        if call.data == "ads_contact_confirm":
            user_ads_state[user_id]["step"] = "ad_text"
            bot.send_message(call.message.chat.id, "📝 أرسل نص إعلانك (سيظهر في القناة):")
        else:
            user_ads_state.pop(user_id, None)
            bot.send_message(
                call.message.chat.id,
                "❌ تم إلغاء عملية الإعلان.",
                reply_markup=types.ReplyKeyboardRemove(),
            )

    # 5️⃣ استقبال نص الإعلان
    @bot.message_handler(
        content_types=["text"],
        func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "ad_text",
    )
    def receive_ad_text(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["ad_text"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "wait_image_option"

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📸 أضف صورة واحدة", callback_data="ads_one_image"))
        markup.add(types.InlineKeyboardButton("🖼️ أضف صورتين", callback_data="ads_two_images"))
        markup.add(types.InlineKeyboardButton("➡️ تخطي الصور", callback_data="ads_skip_images"))

        bot.send_message(
            msg.chat.id,
            "🖼️ يمكنك اختيار إضافة صورة واحدة أو صورتين أو تخطي:",
            reply_markup=markup,
        )

    # 6️⃣ اختيار عدد الصور
    @bot.callback_query_handler(func=lambda call: call.data == "ads_one_image")
    def handle_one_image(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        state = user_ads_state.get(user_id, {})
        state.update({"expect_images": 1, "images": [], "step": "wait_images"})
        user_ads_state[user_id] = state
        bot.send_message(call.message.chat.id, "📸 أرسل صورة واحدة الآن.")

    @bot.callback_query_handler(func=lambda call: call.data == "ads_two_images")
    def handle_two_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        state = user_ads_state.get(user_id, {})
        state.update({"expect_images": 2, "images": [], "step": "wait_images"})
        user_ads_state[user_id] = state
        bot.send_message(call.message.chat.id, "📸 أرسل صورتين الآن واحدة تلو الأخرى.")

    # 7️⃣ استقبال الصور (مع فلتر دقيق لتجنّب التعارض مع recharge)
    @bot.message_handler(
        content_types=["photo", "document"],
        func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "wait_images",
    )
    def receive_images(msg):
        user_id = msg.from_user.id
        state = user_ads_state.get(user_id)

        file_id = None
        if msg.content_type == "photo":
            file_id = msg.photo[-1].file_id
        elif msg.content_type == "document":
            mime_type = getattr(msg.document, "mime_type", "")
            if mime_type and mime_type.startswith("image/"):
                file_id = msg.document.file_id

        if not file_id:
            bot.send_message(msg.chat.id, "❌ الملف المرسل ليس صورة صالحة.")
            return

        state.setdefault("images", []).append(file_id)

        # عند اكتمال العدد المطلوب من الصور
        if len(state["images"]) >= state["expect_images"]:
            state["step"] = "confirm"
            user_ads_state[user_id] = state
            preview_ad(msg, user_id)
        else:
            remaining = state["expect_images"] - len(state["images"])
            bot.send_message(msg.chat.id, f"📸 أرسل الصورة المتبقية ({remaining} متبقية).")

    # 8️⃣ تخطي الصور
    @bot.callback_query_handler(func=lambda call: call.data == "ads_skip_images")
    def skip_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        state = user_ads_state.get(user_id, {})
        state["step"] = "confirm"
        user_ads_state[user_id] = state
        preview_ad(call.message, user_id)

    # 9️⃣ معاينة الإعلان
    def preview_ad(msg, user_id):
        data = user_ads_state.get(user_id)
        if not data:
            bot.send_message(msg.chat.id, "⚠️ انتهت جلسة الإعلان. ابدأ من جديد.")
            return

        # إرسال الصور إذا وُجدت
        if data.get("images"):
            try:
                if len(data["images"]) == 1:
                    bot.send_photo(msg.chat.id, data["images"][0])
                else:
                    media = [types.InputMediaPhoto(p) for p in data["images"]]
                    bot.send_media_group(msg.chat.id, media)
            except Exception:
                bot.send_message(msg.chat.id, "⚠️ تعذّر عرض الصور، سيتم المتابعة بدونها.")

        # نص المعاينة
        ad_preview = (
            "<b><u>📢 إعـــــــلان</u></b>\n\n"
            f"{data['ad_text']}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📱 للتواصل:\n"
            f"{data['contact']}\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد الإعلان", callback_data="ads_confirm_send"),
            types.InlineKeyboardButton("📝 تعديل الإعلان", callback_data
