from telebot import types
from services.wallet_service import get_balance, get_available_balance, create_hold
from services.queue_service import add_pending_request, process_queue
from handlers.keyboards import main_menu 

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

# ==== Helpers للرسائل ====
def _name_from_user(u) -> str:
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

ETA_TEXT = "من 1 إلى 4 دقائق"

# ====================================================================
# التسجيل
# ====================================================================

def register(bot, _history):
    """تسجيل جميع هاندلرات مسار الإعلانات."""

    # ----------------------------------------------------------------
    # 1) مدخل الإعلان – رسالة ترويجية أولية
    # ----------------------------------------------------------------
    @bot.message_handler(func=lambda msg: msg.text == "📢 إعلاناتك")
    def ads_entry(msg):
        name = _name_from_user(msg.from_user)
        promo = (
            "✨ <b>مساحة إعلانات متجرنا</b> ✨\n\n"
            "عبر قناتنا <a href=\"https://t.me/shop100sho\">@shop100sho</a> توصل رسالتك لـ <b>آلاف</b> يوميًا!\n"
            "• روّج لمنتجك أو أسعارك الجديدة\n"
            "• ابحث عن سلعة أو عقار\n"
            "• أعلن عن عقار أو عربية للبيع\n"
            "• انشر فرصة عمل أو دوّر على وظيفة\n\n"
            f"🚀 يا {name}، اضغط «زيارة القناة» تشوف بعينك، وبعدين «متابعة» نكمّل سوا."
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔎 زيارة القناة", url="https://t.me/shop100sho"))
        markup.add(types.InlineKeyboardButton("✅ متابعة", callback_data="ads_start"))
        bot.send_message(msg.chat.id, promo, reply_markup=markup, parse_mode="HTML")

    # ----------------------------------------------------------------
    # 1-bis) متابعة إلى باقات الإعلان
    # ----------------------------------------------------------------
    def send_ads_menu(chat_id):
        mk = types.InlineKeyboardMarkup()
        for text, times, _ in AD_OPTIONS:
            mk.add(types.InlineKeyboardButton(text, callback_data=f"ads_{times}"))
        mk.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ads_back"))
        bot.send_message(chat_id, "🟢 اختار باقتك:", reply_markup=mk)

    @bot.callback_query_handler(func=lambda call: call.data == "ads_start")
    def proceed_to_ads(call):
        bot.answer_callback_query(call.id)
        send_ads_menu(call.message.chat.id)

    @bot.callback_query_handler(func=lambda call: call.data == "ads_back")
    def ads_back(call):
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "رجعناك للقائمة الرئيسية 😎",
            reply_markup=main_menu()
        )

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

        name = _name_from_user(call.from_user)
        bot.send_message(
            call.message.chat.id,
            f"✏️ يا {name}، ابعت وسيلة التواصل (رقم/يوزر/لينك) اللي هتظهر مع الإعلان:"
        )

    # ----------------------------------------------------------------
    # 3) استقبال وسيلة التواصل
    # ----------------------------------------------------------------
    @bot.message_handler(content_types=["text"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "contact")
    def receive_contact(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["contact"] = (msg.text or "").strip()
        user_ads_state[user_id]["step"] = "confirm_contact"

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("تأكيد", callback_data="ads_contact_confirm"),
            types.InlineKeyboardButton("إلغاء", callback_data="ads_cancel")
        )
        bot.send_message(
            msg.chat.id,
            f"📞 هنعرض للتواصل:\n{msg.text}\n\nنكمل؟",
            reply_markup=markup
        )

    # ----------------------------------------------------------------
    # 4) تأكيد وسيلة التواصل أو إلغاء
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in {"ads_contact_confirm", "ads_cancel"})
    def confirm_contact(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        if call.data == "ads_contact_confirm":
            user_ads_state[user_id]["step"] = "ad_text"
            bot.send_message(call.message.chat.id, "📝 ابعت نص إعلانك (هيظهر في القناة):")
        else:
            user_ads_state.pop(user_id, None)
            bot.send_message(call.message.chat.id, "❌ اتلغت عملية الإعلان. نورتنا 🙏", reply_markup=types.ReplyKeyboardRemove())

    # ----------------------------------------------------------------
    # 5) استقبال نص الإعلان
    # ----------------------------------------------------------------
    @bot.message_handler(content_types=["text"], func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "ad_text")
    def receive_ad_text(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["ad_text"] = (msg.text or "").strip()
        user_ads_state[user_id]["step"] = "wait_image_option"

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("📸 أضف صورة واحدة", callback_data="ads_one_image"),
            types.InlineKeyboardButton("🖼️ أضف صورتين", callback_data="ads_two_images"),
            types.InlineKeyboardButton("➡️ تخطي الصور", callback_data="ads_skip_images")
        )
        bot.send_message(msg.chat.id, "🖼️ عايز تضيف صور؟ اختار:", reply_markup=markup)

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
        bot.send_message(call.message.chat.id, "📸 ابعت الصورة دلوقتي." if expect == 1 else "📸 ابعت الصورتين وراء بعض.")

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
            bot.send_message(msg.chat.id, "❌ الملف ده مش صورة صالحة.")
            return

        state.setdefault("images", []).append(file_id)

        if len(state["images"]) >= state["expect_images"]:
            state["step"] = "confirm"
            preview_ad(msg.chat.id, user_id)
        else:
            remaining = state["expect_images"] - len(state["images"])
            bot.send_message(msg.chat.id, f"📸 فاضللك {remaining} صورة.")

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
            bot.send_message(chat_id, "⚠️ الجلسة خلصت. نبدأ من جديد؟")
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
                bot.send_message(chat_id, "⚠️ معرفناش نعرض الصور، هنكمّل بدونها.")

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
            types.InlineKeyboardButton("📝 تعديل الإعلان", callback_data="ads_edit"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="ads_cancel")
        )
        bot.send_message(chat_id, ad_preview, reply_markup=markup, parse_mode="HTML")

    # ----------------------------------------------------------------
    # 10) تعديل الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_edit")
    def edit_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_ads_state[user_id]["step"] = "ad_text"
        bot.send_message(call.message.chat.id, "🔄 عدّل نص إعلانك أو ابعت نص جديد:")

    # ----------------------------------------------------------------
    # 11) إلغاء الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_cancel")
    def cancel_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_ads_state.pop(user_id, None)
        bot.send_message(call.message.chat.id, "❌ اتلغت عملية الإعلان. نورتنا 🙏", reply_markup=types.ReplyKeyboardRemove())

    # ----------------------------------------------------------------
    # 12) تأكيد الإعلان (إرساله للطابور)
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_confirm_send")
    def confirm_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        data = user_ads_state.get(user_id)

        # التحقق من المرحلة
        if not data or data.get("step") != "confirm":
            bot.send_message(call.message.chat.id, "⚠️ الجلسة خلصت أو حصل لخبطة. جرّب من الأول.")
            user_ads_state.pop(user_id, None)
            return

        price   = int(data["price"])
        times   = int(data["times"])
        balance = int(get_balance(user_id) or 0)
        name    = _name_from_user(call.from_user)

        # ✅ الرصيد المتاح (balance - held)
        available = int(get_available_balance(user_id) or 0)
        if available < price:
            missing = price - available
            bot.send_message(
                call.message.chat.id,
                f"❌ يا {name}، رصيدك المتاح مش كفاية للبـاقة.\n"
                f"المتاح: <b>{_fmt_syp(available)}</b>\n"
                f"السعر: <b>{_fmt_syp(price)}</b>\n"
                f"الناقص تقريبًا: <b>{_fmt_syp(missing)}</b>",
                parse_mode="HTML"
            )
            return

        # ——— حجز المبلغ عبر RPC ———
        try:
            hold_resp = create_hold(user_id, price)
            if getattr(hold_resp, "error", None) or not getattr(hold_resp, "data", None):
                bot.send_message(
                    call.message.chat.id,
                    f"❌ يا {name}، حصلت مشكلة أثناء الحجز. جرّب بعد شوية.",
                )
                return
            hold_id = hold_resp.data  # UUID
        except Exception:
            bot.send_message(
                call.message.chat.id,
                f"❌ يا {name}، حصلت مشكلة أثناء الحجز. جرّب بعد شوية.",
            )
            return

        # ===== رسالة الأدمن بالقالب الموحّد =====
        id_value = (data.get("contact") or "").strip() or "—"
        admin_msg = (
            f"💰 رصيد المستخدم: {balance:,} ل.س\n"
            f"🆕 طلب جديد\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"آيدي اللاعب: <code>{id_value}</code>\n"
            f"🔖 المنتج: إعلان مدفوع × {times}\n"
            f"التصنيف: إعلانات\n"
            f"💵 السعر: {price:,} ل.س\n"
            f"(ads_{times})"
        )

        # إنشاء الـ payload
        payload = {
            "type": "ads",
            "count": times,
            "price": price,
            "contact": data.get("contact"),
            "ad_text": data.get("ad_text"),
            "images": data.get("images", []),
            "reserved": price,    # مبلغ محجوز
            "hold_id": hold_id,   # للقبول/الإلغاء
        }

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload=payload,
        )

        # معالجة فورية لو في أدمن متصل
        process_queue(bot)

        bot.send_message(
            user_id,
            f"✅ تمام يا {name}! بعتنا إعلانك للإدارة.\n"
            f"⏱️ سيتم تنفيذ الطلب {ETA_TEXT}.\n"
            f"حجزنا <b>{_fmt_syp(price)}</b> من محفظتك مؤقتًا لباقة الإعلان (×{times}).",
            parse_mode="HTML"
        )
        user_ads_state.pop(user_id, None)
