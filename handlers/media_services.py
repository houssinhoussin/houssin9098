# handlers/media_services.py
from telebot import types
from services.wallet_service import register_user_if_not_exist, get_available_balance, get_balance, create_hold
from services.queue_service import add_pending_request, process_queue
from handlers.keyboards import media_services_menu
import logging

# 🎨 رسومات بسيطة
BAND = "━━━━━━━━━━━━━━━━━━━━"

# حالة المستخدم داخل سير عمل خدمات الإعلام
user_media_state = {}

USD_RATE = 11000  # سعر الصرف ليرة/دولار
MEDIA_PRODUCTS = {
    "🖼️ تصميم لوغو احترافي": 300,
    "📱 إدارة ونشر يومي": 300,
    "📢 إطلاق حملة إعلانية": 300,
    "🎬 مونتاج فيديو قصير": 150,
    "🧵 خيوط تويتر جاهزة": 80,
    "🎙️ تعليق صوتي احترافي": 120,
    "📰 كتابة محتوى تسويقي": 95,
}

def _name(u):
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n):
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _fmt_usd(x):
    try:
        return f"${float(x):.2f}"
    except Exception:
        return f"${x}"

def register_media_services(bot, history):
    @bot.message_handler(func=lambda msg: msg.text == "🎭 خدمات سوشيال/ميديا")
    def open_media(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, _name(msg.from_user))
        if history is not None:
            history.setdefault(user_id, []).append("media_menu")
        text = (
            f"🎯 يا {_name(msg.from_user)}، اختار الخدمة الإعلامية اللي تناسبك:\n"
            f"{BAND}"
        )
        bot.send_message(
            msg.chat.id,
            text,
            reply_markup=media_services_menu()
        )

    @bot.message_handler(func=lambda msg: msg.text in MEDIA_PRODUCTS)
    def handle_selected_service(msg):
        user_id = msg.from_user.id
        service = msg.text
        price_usd = MEDIA_PRODUCTS[service]
        price_syp = int(price_usd * USD_RATE)

        user_media_state[user_id] = {
            "step": "confirm_service",
            "service": service,
            "price_usd": price_usd,
            "price_syp": price_syp
        }
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ تمام.. أكّد الطلب", callback_data="media_final_confirm"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="media_cancel")
        )
        text = (
            f"✨ اختيار هايل يا {_name(msg.from_user)}!\n"
            f"• الخدمة: {service}\n"
            f"• السعر: {_fmt_usd(price_usd)} ≈ {_fmt_syp(price_syp)}\n"
            f"{BAND}\n"
            "لو تمام، أكّد الطلب وهنبعته على طول للإدارة."
        )
        bot.send_message(
            msg.chat.id,
            text,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda c: c.data == "media_cancel")
    def media_cancel(c):
        user_media_state.pop(c.from_user.id, None)
        bot.answer_callback_query(c.id, "تم الإلغاء.")
        bot.send_message(c.from_user.id, "❌ تم الإلغاء. رجّعنا للقائمة ✨", reply_markup=media_services_menu())

    @bot.callback_query_handler(func=lambda c: c.data == "media_final_confirm")
    def media_final_confirm(c):
        user_id = c.from_user.id
        name = _name(c.from_user)
        state = user_media_state.get(user_id) or {}

        service = state.get("service")
        price_syp = int(state.get("price_syp") or 0)
        price_usd = state.get("price_usd")

        if not service or price_syp <= 0:
            return bot.answer_callback_query(c.id, "❌ الطلب ناقص. جرّب تاني.")

        # ✅ الرصيد المتاح فقط
        available = get_available_balance(user_id)
        if available < price_syp:
            text = (
                f"❌ يا {name}، رصيدك المتاح مش مكفّي.\n"
                f"المتاح: {_fmt_syp(available)}\n"
                f"السعر: {_fmt_syp(price_syp)}\n"
                "اشحن المحفظة وبعدين كمّل الطلب 😉"
            )
            return bot.send_message(user_id, text)

        # ✅ إنشاء حجز (Hold) ذري
        hold_id = None
        try:
            resp = create_hold(user_id, price_syp, f"حجز خدمة ميديا — {service}")
            if getattr(resp, "error", None):
                logging.error("create_hold (media) error: %s", resp.error)
                return bot.send_message(user_id, "⚠️ حصل عطل بسيط أثناء الحجز. جرّب بعد دقيقة.")
            hold_id = getattr(resp, "data", None) or (resp.get("id") if isinstance(resp, dict) else None)
        except Exception as e:
            logging.exception("create_hold (media) exception: %s", e)
            return bot.send_message(user_id, "⚠️ حصل خطأ أثناء الحجز. جرّب بعد شوية.")

        if not hold_id:
            return bot.send_message(user_id, "⚠️ الحجز ما تمّش. حاول تاني لو سمحت.")

        # رسالة موحّدة للإدارة + تفاصيل الحجز
        balance_now = get_balance(user_id)
        admin_text = (
            f"💰 رصيد المستخدم: {balance_now:,} ل.س\n"
            f"🆕 طلب ميديا\n"
            f"👤 الاسم: <code>{c.from_user.full_name}</code>\n"
            f"يوزر: <code>@{c.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🎭 الخدمة: {service}\n"
            f"💵 السعر: {price_syp:,} ل.س (≈ {_fmt_usd(price_usd)})\n"
            f"(type=media)"
        )

        add_pending_request(
            user_id=user_id,
            username=c.from_user.username,
            request_text=admin_text,
            payload={
                "type": "media",
                "service": service,
                "price": price_syp,
                "reserved": price_syp,
                "hold_id": hold_id
            }
        )
        process_queue(bot)
        bot.answer_callback_query(c.id, "تم الإرسال 🚀")
        user_text = (
            f"✅ تمام يا {name}! بعتنا طلب «{service}» للإدارة.\n"
            f"⏱️ التنفيذ بيتم خلال 1–4 دقايق (غالبًا أسرع 😉).\n"
            f"{BAND}\n"
            "ممكن تطلب خدمة تانية في نفس الوقت — بنحجز من المتاح بس."
        )
        bot.send_message(user_id, user_text)

def register(bot, history):
    register_media_services(bot, history)
