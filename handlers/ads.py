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

user_ads_state = {}

def register(bot, history):

    @bot.message_handler(func=lambda msg: msg.text == "📢 إعلاناتك")
    def open_ads_menu(msg):
        markup = types.InlineKeyboardMarkup()
        for text, times, price in AD_OPTIONS:
            markup.add(types.InlineKeyboardButton(text, callback_data=f"ads_{times}"))
        markup.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ads_back"))
        bot.send_message(msg.chat.id, "🟢 اختر نوع إعلانك:", reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("ads_") and call.data[4:].isdigit())
    def select_ad_type(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        times = int(call.data.split("_")[1])
        for text, t, price in AD_OPTIONS:
            if t == times:
                user_ads_state[user_id] = {
                    "times": times,
                    "price": price,
                    "step": "contact",
                }
                break
        bot.send_message(call.message.chat.id, "✏️ أرسل رقم التواصل، صفحتك أو موقعك (سيظهر للإعلان):")

    @bot.message_handler(content_types=["text"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "contact")
    def receive_contact(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["contact"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "ad_text"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("تأكيد", callback_data="ads_contact_confirm"))
        markup.add(types.InlineKeyboardButton("إلغاء", callback_data="ads_cancel"))
        bot.send_message(msg.chat.id, f"📞 سيتم عرض للتواصل:
{msg.text}

هل تريد المتابعة؟", reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data in ["ads_contact_confirm", "ads_cancel"])
    def confirm_contact(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        if call.data == "ads_contact_confirm":
            user_ads_state[user_id]["step"] = "ad_text"
            bot.send_message(call.message.chat.id, "📝 أرسل نص إعلانك (سيظهر في القناة):")
        else:
            user_ads_state.pop(user_id, None)
            bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    @bot.message_handler(content_types=["text"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "ad_text")
    def receive_ad_text(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["ad_text"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "wait_image_option"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📸 أضف صورة واحدة", callback_data="ads_one_image"))
        markup.add(types.InlineKeyboardButton("🖼️ أضف صورتين", callback_data="ads_two_images"))
        markup.add(types.InlineKeyboardButton("➡️ تخطي الصور", callback_data="ads_skip_images"))
        bot.send_message(msg.chat.id, "🖼️ يمكنك اختيار إضافة صورة واحدة أو صورتين أو تخطي:", reply_markup=markup)

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

    @bot.message_handler(content_types=["photo", "document"])
    def receive_images(msg):
        user_id = msg.from_user.id
        state = user_ads_state.get(user_id, {})
        if state.get("step") != "wait_images":
            return
        file_id = None
        if msg.content_type == "photo":
            file_id = msg.photo[-1].file_id
        elif msg.content_type == "document" and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
            file_id = msg.document.file_id
        if file_id is None:
            return
        state.setdefault("images", []).append(file_id)
        if len(state["images"]) >= state["expect_images"]:
            state["step"] = "confirm"
            user_ads_state[user_id] = state
            preview_ad(msg, user_id)
        else:
            remaining = state["expect_images"] - len(state["images"])
            bot.send_message(msg.chat.id, f"📸 أرسل الصورة المتبقية ({remaining} متبقية).")
        user_ads_state[user_id] = state

    @bot.callback_query_handler(func=lambda call: call.data == "ads_skip_images")
    def skip_images(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        preview_ad(call.message, user_id)
        state = user_ads_state.get(user_id, {})
        state["step"] = "confirm"
        user_ads_state[user_id] = state

    def preview_ad(msg, user_id):
        data = user_ads_state.get(user_id)
        if not data:
            bot.send_message(msg.chat.id, "⚠️ انتهت جلسة الإعلان. ابدأ من جديد.")
            return
        if data.get("images"):
            try:
                if len(data["images"]) == 1:
                    bot.send_photo(msg.chat.id, data["images"][0])
                else:
                    media = [types.InputMediaPhoto(p) for p in data["images"]]
                    bot.send_media_group(msg.chat.id, media)
            except Exception as e:
                print("Media error:", e)
                bot.send_message(msg.chat.id, "⚠️ تعذّر عرض الصور، سيتم المتابعة بدونها.")
        ad_preview = (
            "🚀✨✨ إعلان مميز من المتجر العالمي ✨✨🚀

"
            f"{data['ad_text']}
"
            "━━━━━━━━━━━━━━━━━━
"
            "📱 للتواصل:
"
            f"{data['contact']}
"
            "━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد الإعلان", callback_data="ads_confirm_send"),
            types.InlineKeyboardButton("📝 تعديل الإعلان", callback_data="ads_edit"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="ads_cancel"),
        )
        bot.send_message(msg.chat.id, ad_preview, reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data == "ads_edit")
    def edit_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_ads_state[user_id]["step"] = "ad_text"
        bot.send_message(call.message.chat.id, "🔄 عدل نص إعلانك أو أرسل إعلان جديد:")

    @bot.callback_query_handler(func=lambda call: call.data == "ads_cancel")
    def cancel_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_ads_state.pop(user_id, None)
        bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    @bot.callback_query_handler(func=lambda call: call.data == "ads_confirm_send")
    def confirm_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        data = user_ads_state.get(user_id)
        if not data or data.get("step") != "confirm":
            bot.send_message(call.message.chat.id, "⚠️ انتهت الجلسة أو حصل خطأ. أعد البدء.")
            user_ads_state.pop(user_id, None)
            return
        price = data["price"]
        balance = get_balance(user_id)
        if balance is None or balance < price:
            missing = price - (balance or 0)
            bot.send_message(call.message.chat.id, f"❌ رصيدك غير كافٍ لهذا الإعلان.
الناقص: {missing:,} ل.س")
            return
        deduct_balance(user_id, price)
        payload = {
            "type": "ads",
            "count": data["times"],
            "price": data["price"],
            "contact": data["contact"],
            "ad_text": data["ad_text"],
            "images": data.get("images", []),
        }
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text="إعلان جديد بانتظار الموافقة",
            payload=payload,
        )
        process_queue(bot)
        bot.send_message(user_id, "✅ تم إرسال إعلانك إلى الإدارة لمراجعتها قبل النشر.")
        user_ads_state.pop(user_id, None)
