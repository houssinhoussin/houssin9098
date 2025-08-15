# -*- coding: utf-8 -*-
# handlers/ads.py — نظام الإعلانات داخل البوت
# • /cancel للإلغاء في أي وقت
# • confirm_guard عند التأكيد (يحذف الكيبورد فقط + Debounce)
# • رسائل محسّنة وإيموجي وبانر
# • حجز المبلغ عبر create_hold مع وصف واضح
# • فحص الصيانة + إمكانية إيقاف الخدمة عبر Feature Flag (ads)

from telebot import types

from services.wallet_service import (
    get_balance,
    get_available_balance,
    create_hold,
    register_user_if_not_exist,
)
from services.queue_service import add_pending_request, process_queue
from handlers.keyboards import main_menu

# === Publisher used by services/scheduled_tasks.post_ads_task ===
from config import CHANNEL_USERNAME
from telebot.types import InputMediaPhoto
import html

def _prep_channel_id():
    cid = CHANNEL_USERNAME or ""
    cid = cid.strip()
    # قبول @username أو -100id
    if cid.startswith("@") or cid.startswith("-100"):
        return cid
    if cid:
        return f"@{cid}"
    raise RuntimeError("CHANNEL_USERNAME غير مضبوط في config.py")

def _safe_html(s: str) -> str:
    try:
        return html.escape(str(s or ""))
    except Exception:
        return str(s or "")

def publish_channel_ad(bot, ad_row) -> bool:
    """
    تنشر إعلانًا واحدًا في قناة CHANNEL_USERNAME.
    ad_row يحتوي: ad_text, contact, images (قائمة file_id), ...
    ترجع True عند النجاح، False عند الفشل (حتى لا يُزاد العداد).
    """
    chat_id = _prep_channel_id()
    ad_text  = _safe_html(ad_row.get("ad_text") or "")
    contact  = _safe_html(ad_row.get("contact") or "—")
    images   = [x for x in (ad_row.get("images") or []) if x]

    # نص الرسالة
    body = (
        "<b><u>📣 إعـــــلان</u></b>\n\n"
        f"{ad_text}\n"
        "━━━━━━━━━━━━━━━━\n"
        "📱 للتواصل:\n"
        f"{contact}\n"
        "━━━━━━━━━━━━━━━━"
    )

    try:
        if images:
            # صورة واحدة → caption + HTML
            if len(images) == 1:
                cap = body[:1000]  # نحجز ~24 حرف احتياط للكابتشن
                bot.send_photo(chat_id, images[0], caption=cap, parse_mode="HTML")
                if len(body) > len(cap):
                    bot.send_message(chat_id, body, parse_mode="HTML")
            else:
                # أكثر من صورة → media group: أول صورة معها Caption
                media = [InputMediaPhoto(images[0], caption=body[:1000], parse_mode="HTML")]
                media += [InputMediaPhoto(x) for x in images[1:10]]  # أقصى 10 حسب تيليجرام
                bot.send_media_group(chat_id, media)
                if len(body) > 1000:
                    bot.send_message(chat_id, body, parse_mode="HTML")
        else:
            bot.send_message(chat_id, body, parse_mode="HTML")
        return True
    except Exception as e:
        # خليه False عشان الجدولة تعيد المحاولة وما تزود العداد
        print(f"[publish_channel_ad] failed: {e}")
        return False


# صيانة + أعلام المزايا
from services.system_service import is_maintenance, maintenance_message
from services.feature_flags import block_if_disabled  # requires flag key: "ads"

# حارس التأكيد الموحّد (يحذف الكيبورد + يمنع الدبل-كليك)
try:
    from services.ui_guards import confirm_guard
except Exception:
    from ui_guards import confirm_guard

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
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."
ETA_TEXT = "من 1 إلى 4 دقائق"

def banner(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def with_cancel_hint(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _name_from_user(u) -> str:
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

# ====================================================================
# التسجيل
# ====================================================================

def register(bot, _history):
    """تسجيل جميع هاندلرات مسار الإعلانات."""

    # ===== /cancel العام =====
    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(msg):
        uid = msg.from_user.id
        user_ads_state.pop(uid, None)
        bot.send_message(
            msg.chat.id,
            banner("❌ تم الإلغاء", [f"يا {_name_from_user(msg.from_user)}، رجعناك للقائمة الرئيسية 👇"]),
            reply_markup=main_menu()
        )

    # ----------------------------------------------------------------
    # 1) مدخل الإعلان – رسالة ترويجية أولية
    # ----------------------------------------------------------------
    @bot.message_handler(func=lambda msg: msg.text == "📢 إعلاناتك")
    def ads_entry(msg):
        # صيانة/إيقاف خدمة؟
        if is_maintenance():
            return bot.send_message(msg.chat.id, maintenance_message())
        if block_if_disabled(bot, msg.chat.id, "ads", "خدمة الإعلانات"):
            return

        # تسجيل المستخدم (لإنشاء الحساب إن لم يوجد)
        register_user_if_not_exist(msg.from_user.id, msg.from_user.full_name)

        name = _name_from_user(msg.from_user)
        promo = with_cancel_hint(
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
        bot.send_message(chat_id, with_cancel_hint("🟢 اختار باقتك:"), reply_markup=mk)

    @bot.callback_query_handler(func=lambda call: call.data == "ads_start")
    def proceed_to_ads(call):
        # صيانة/إيقاف خدمة؟
        if is_maintenance():
            bot.answer_callback_query(call.id)
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "ads", "خدمة الإعلانات"):
            return bot.answer_callback_query(call.id)
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
        # صيانة/إيقاف خدمة؟
        if is_maintenance():
            bot.answer_callback_query(call.id)
            return bot.send_message(call.message.chat.id, maintenance_message())
        if block_if_disabled(bot, call.message.chat.id, "ads", "خدمة الإعلانات"):
            return bot.answer_callback_query(call.id)

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
            with_cancel_hint(f"✏️ يا {name}، ابعت وسيلة التواصل (رقم/يوزر/لينك) اللي هتظهر مع الإعلان:")
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
            types.InlineKeyboardButton("✅ تأكيد", callback_data="ads_contact_confirm"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="ads_cancel")
        )
        bot.send_message(
            msg.chat.id,
            with_cancel_hint(f"📞 هنعرض للتواصل:\n{msg.text}\n\nنكمل؟"),
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
            bot.send_message(call.message.chat.id, with_cancel_hint("📝 ابعت نص إعلانك (هيظهر في القناة):"))
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
        bot.send_message(msg.chat.id, with_cancel_hint("🖼️ عايز تضيف صور؟ اختار:"), reply_markup=markup)

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
        bot.send_message(call.message.chat.id, with_cancel_hint("📸 ابعت الصورة دلوقتي." if expect == 1 else "📸 ابعت الصورتين وراء بعض."))

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
        bot.send_message(chat_id, with_cancel_hint(ad_preview), reply_markup=markup, parse_mode="HTML")

    # ----------------------------------------------------------------
    # 10) تعديل الإعلان
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_edit")
    def edit_ad(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        user_ads_state[user_id]["step"] = "ad_text"
        bot.send_message(call.message.chat.id, with_cancel_hint("🔄 عدّل نص إعلانك أو ابعت نص جديد:"))

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
    # 12) تأكيد الإعلان (إرساله للطابور مع حجز المبلغ)
    # ----------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "ads_confirm_send")
    def confirm_ad(call):
        user_id = call.from_user.id

        # ✅ عند التأكيد — احذف الكيبورد فقط + Debounce (يمنع الدبل-كليك)
        if confirm_guard(bot, call, "ads_confirm_send"):
            return

        # صيانة/إيقاف خدمة؟
        if is_maintenance():
            bot.send_message(call.message.chat.id, maintenance_message())
            return
        if block_if_disabled(bot, call.message.chat.id, "ads", "خدمة الإعلانات"):
            return

        data = user_ads_state.get(user_id)

        # التحقق من المرحلة
        if not data or data.get("step") != "confirm":
            bot.send_message(call.message.chat.id, "⚠️ الجلسة خلصت أو حصل لخبطة. جرّب من الأول.")
            user_ads_state.pop(user_id, None)
            return

        price = int(data["price"])
        times = int(data["times"])
        name = _name_from_user(call.from_user)

        # ✅ الرصيد المتاح (balance - held)
        available = int(get_available_balance(user_id) or 0)
        if available < price:
            missing = price - available
            bot.send_message(
                call.message.chat.id,
                with_cancel_hint(
                    f"❌ يا {name}، رصيدك المتاح مش كفاية للبـاقة.\n"
                    f"المتاح: <b>{_fmt_syp(available)}</b>\n"
                    f"السعر: <b>{_fmt_syp(price)}</b>\n"
                    f"الناقص تقريبًا: <b>{_fmt_syp(missing)}</b>"
                ),
                parse_mode="HTML",
            )
            return

        # 🧾 إنشاء حجز للمبلغ (ذرّيًا عبر RPC)
        hold_id = create_hold(user_id, price, f"ads x{times}")
        if not hold_id:
            bot.send_message(call.message.chat.id, "❌ تعذر حجز المبلغ. حاول لاحقًا.")
            return

        # 📨 إضافة الطلب لطابور الإدارة
        payload = {
            "type": "ads",
            "times": times,
            "price": price,
            "contact": data.get("contact"),
            "ad_text": data.get("ad_text"),
            "images": data.get("images") or [],
            "user_id": user_id,
            "reserved": price,
            "hold_id": hold_id,
            "hold_desc": f"ads x{times}",
        }
        add_pending_request(user_id, "ads", payload, f"طلب إعلان ×{times} بسعر {_fmt_syp(price)}")
        process_queue(bot)

        # ✔️ إنهاء الواجهة وإعلام المستخدم
        safe_finalize(
            bot,
            call.message,
            new_text="✅ تم إرسال طلب إعلانك للمراجعة. سنبلغك حال الموافقة.",
            parse_mode=None,
        )
        user_ads_state[user_id] = {"step": "submitted"}


        # ——— حجز المبلغ عبر RPC ———
        hold_id = None
        try:
            hold_desc = f"حجز إعلان مدفوع × {times}"
            hold_resp = create_hold(user_id, price, hold_desc)
            if getattr(hold_resp, "error", None):
                bot.send_message(
                    call.message.chat.id,
                    with_cancel_hint(f"❌ يا {name}، حصلت مشكلة أثناء الحجز. جرّب بعد شوية."),
                )
                return
            # استخراج hold_id بمرونة (dict/list/primitive)
            data_attr = getattr(hold_resp, "data", None)
            if isinstance(data_attr, dict):
                hold_id = data_attr.get("id") or data_attr.get("hold_id") or data_attr
            elif isinstance(data_attr, (list, tuple)) and data_attr:
                first = data_attr[0]
                hold_id = first.get("id") if isinstance(first, dict) else first
            else:
                hold_id = data_attr
            if not hold_id:
                bot.send_message(
                    call.message.chat.id,
                    with_cancel_hint(f"❌ يا {name}، فشل إنشاء الحجز. جرّب بعد دقيقة."),
                )
                return
        except Exception:
            bot.send_message(
                call.message.chat.id,
                with_cancel_hint(f"❌ يا {name}، حصلت مشكلة أثناء الحجز. جرّب بعد شوية."),
            )
            return

        # ===== رسالة الأدمن بالقالب الموحّد =====
        balance_now = int(get_balance(user_id) or 0)
        id_value = (data.get("contact") or "").strip() or "—"
        admin_msg = (
            f"💰 رصيد المستخدم: {balance_now:,} ل.س\n"
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
            "reserved": price,      # مبلغ محجوز
            "hold_id": hold_id,     # للقبول/الإلغاء من لوحة الأدمن
            "hold_desc": hold_desc, # وصف للتتبع
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
            banner(
                f"✅ تمام يا {name}! إعلانك اتبعت للإدارة 🚀",
                [
                    f"⏱️ التنفيذ عادةً {ETA_TEXT}.",
                    f"🔒 حجزنا {_fmt_syp(price)} مؤقتًا لباقة الإعلان (×{times}).",
                ]
            ),
            parse_mode="HTML"
        )
        user_ads_state.pop(user_id, None)
