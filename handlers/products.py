# handlers/products.py

from services.products_admin import is_product_active
import logging
from database.db import get_table
from telebot import types
from services.system_service import is_maintenance, maintenance_message
from services.wallet_service import register_user_if_not_exist, get_balance, get_available_balance, create_hold
from config import BOT_NAME
from handlers import keyboards
from services.queue_service import process_queue, add_pending_request
from database.models.product import Product

# ==== Helpers للرسائل الموحدة ====
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

# حالة الطلبات لكل مستخدم
user_orders = {}

def has_pending_request(user_id: int) -> bool:
    """ترجع True إذا كان لدى المستخدم طلب قيد الانتظار."""
    res = (
        get_table("pending_requests")
        .select("id")
        .eq("user_id", user_id)
        .execute()
    )
    return bool(res.data)

# ============= تعريف المنتجات =============

def make_product(pid: int, amount: int, unit: str, usd: float, category: str = "ألعاب") -> Product:
    name = f"{amount} {unit}"
    # نخلي نص الزر = نفس اسم المنتج بالضبط
    return Product(pid, name, category, usd, name)

PRODUCTS = {
    "PUBG": [
        make_product(1, 60,   "شدة",   0.89),
        make_product(2, 325,  "شدة",   4.44),
        make_product(3, 660,  "شدة",   8.85),
        make_product(4, 1800, "شدة",  22.09),
        make_product(5, 3850, "شدة",  43.24),
        make_product(6, 8100, "شدة",  86.31),
    ],
    "FreeFire": [
        # ملاحظة: لو تحبها "جوهر" بدل "جوهرة" قولي أبدّلها لك في سطر واحد
        make_product(7,   100,  "جوهرة",  0.98),
        make_product(8,   310,  "جوهرة",  2.49),
        make_product(9,   520,  "جوهرة",  4.13),
        make_product(10, 1060,  "جوهرة",  9.42),
        make_product(11, 2180,  "جوهرة", 18.84),
    ],
    "Jawaker": [
        make_product(12,  10000, "توكينز",  1.34),
        make_product(13,  15000, "توكينز",  2.01),
        make_product(14,  20000, "توكينز",  2.68),
        make_product(15,  30000, "توكينز",  4.02),
        make_product(16,  60000, "توكينز",  8.04),
        make_product(17, 120000, "توكينز", 16.08),
    ],
}


def convert_price_usd_to_syp(usd):
    if usd <= 5:
        return int(usd * 11800)
    elif usd <= 10:
        return int(usd * 11600)
    elif usd <= 20:
        return int(usd * 11300)
    return int(usd * 11000)

# ================= واجهات العرض =================

def show_products_menu(bot, message):
    name = _name_from_user(message.from_user)
    bot.send_message(message.chat.id, f"📍 أهلاً {name}! اختار نوع المنتج اللي يناسبك 😉", reply_markup=keyboards.products_menu())

def show_game_categories(bot, message):
    name = _name_from_user(message.from_user)
    bot.send_message(message.chat.id, f"🎮 يا {name}، اختار اللعبة أو التطبيق اللي محتاجه:", reply_markup=keyboards.game_categories())

def show_product_options(bot, message, category):
    options = PRODUCTS.get(category, [])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    # اسم الزر = اسم المنتج بالضبط
    for p in options:
        keyboard.add(types.InlineKeyboardButton(p.name, callback_data=f"select_{p.product_id}"))
    keyboard.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_categories"))
    bot.send_message(message.chat.id, f"📦 منتجات {category}: اختار اللي على مزاجك 😎", reply_markup=keyboard)

# ================= خطوات إدخال آيدي اللاعب =================

def handle_player_id(message, bot):
    user_id = message.from_user.id
    player_id = (message.text or "").strip()
    name = _name_from_user(message.from_user)

    order = user_orders.get(user_id)
    if not order or "product" not in order:
        bot.send_message(user_id, f"❌ {name}، ما عندنا طلب شغّال دلوقتي. اختار المنتج وابدأ من جديد.")
        return

    order["player_id"] = player_id
    product = order["product"]
    price_syp = convert_price_usd_to_syp(product.price)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("✅ تمام.. أكّد الطلب", callback_data="final_confirm_order"),
        types.InlineKeyboardButton("✏️ أعدّل الآيدي", callback_data="edit_player_id"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_order")
    )

    bot.send_message(
        user_id,
        (
            f"تمام يا {name}! 👌\n"
            f"• المنتج: {product.name}\n"
            f"• الفئة: {product.category}\n"
            f"• السعر: {_fmt_syp(price_syp)}\n"
            f"• آيدي اللاعب: {player_id}\n\n"
            f"هنبعت الطلب للإدارة والخصم هيتم بعد الموافقة.\n"
            f"بعد التأكيد مش هتقدر تبعت طلب جديد غير لما نخلّص الحالي."
        ),
        reply_markup=keyboard
    )

# ================= تسجيل هاندلرات الرسائل =================

def register_message_handlers(bot, history):
    @bot.message_handler(func=lambda msg: msg.text in ["🛒 المنتجات", "💼 المنتجات"])
    def handle_main_product_menu(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)
        val = history.get(user_id)
        if val is None:
            history[user_id] = ["products_menu"]
        elif isinstance(val, list):
            history[user_id].append("products_menu")
        elif isinstance(val, str):
            history[user_id] = [val, "products_menu"]
        else:
            history[user_id] = ["products_menu"]

        show_products_menu(bot, msg)

    @bot.message_handler(func=lambda msg: msg.text == "🎮 شحن ألعاب و تطبيقات")
    def handle_games_menu(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)
        val = history.get(user_id)
        if val is None:
            history[user_id] = ["games_menu"]
        elif isinstance(val, list):
            history[user_id].append("games_menu")
        elif isinstance(val, str):
            history[user_id] = [val, "games_menu"]
        else:
            history[user_id] = ["games_menu"]
        show_game_categories(bot, msg)

    @bot.message_handler(func=lambda msg: msg.text in [
        "🎯 شحن شدات ببجي العالمية",
        "🔥 شحن جواهر فري فاير",
        "🏏 تطبيق جواكر"
    ])
    def game_handler(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)

        if is_maintenance():
            try:
                bot.send_message(msg.chat.id, maintenance_message())
            finally:
                return

        category_map = {
            "🎯 شحن شدات ببجي العالمية": "PUBG",
            "🔥 شحن جواهر فري فاير": "FreeFire",
            "🏏 تطبيق جواكر": "Jawaker"
        }
        category = category_map[msg.text]
        history.setdefault(user_id, []).append("product_options")
        user_orders[user_id] = {"category": category}
        show_product_options(bot, msg, category)

# ================= تسجيل هاندلرات الكولباك =================

def setup_inline_handlers(bot, admin_ids):
    @bot.callback_query_handler(func=lambda c: c.data.startswith("select_"))
    def on_select_product(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        product_id = int(call.data.split("_", 1)[1])

        # ابحث عن المنتج
        selected = None
        for items in PRODUCTS.values():
            for p in items:
                if p.product_id == product_id:
                    selected = p
                    break
            if selected:
                break
        if not selected:
            return bot.answer_callback_query(call.id, f"❌ {name}، المنتج مش موجود. جرّب تاني.")

        # ✅ منع اختيار منتج موقوف
        if not is_product_active(product_id):
            return bot.answer_callback_query(call.id, f"⛔ {name}، المنتج متوقّف مؤقتًا.")

        user_orders[user_id] = {"category": selected.category, "product": selected}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_products"))
        msg = bot.send_message(user_id, f"💡 يا {name}، ابعت آيدي اللاعب لو سمحت:", reply_markup=kb)
        bot.register_next_step_handler(msg, handle_player_id, bot)

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_products")
    def back_to_products(call):
        user_id = call.from_user.id
        category = user_orders.get(user_id, {}).get("category")
        if category:
            show_product_options(bot, call.message, category)

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_categories")
    def back_to_categories(call):
        show_game_categories(bot, call.message)

    @bot.callback_query_handler(func=lambda c: c.data == "cancel_order")
    def cancel_order(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        user_orders.pop(user_id, None)
        bot.send_message(user_id, f"❌ تم إلغاء الطلب يا {name}. بنجهّزلك عروض أحلى المرة الجاية 🤝", reply_markup=keyboards.products_menu())

    @bot.callback_query_handler(func=lambda c: c.data == "edit_player_id")
    def edit_player_id(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_products"))
        msg = bot.send_message(user_id, f"📋 يا {name}، ابعت آيدي اللاعب الجديد:", reply_markup=kb)
        bot.register_next_step_handler(msg, handle_player_id, bot)

    @bot.callback_query_handler(func=lambda c: c.data == "final_confirm_order")
    def final_confirm_order(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        order = user_orders.get(user_id)
        if not order or "product" not in order or "player_id" not in order:
            return bot.answer_callback_query(call.id, f"❌ {name}، الطلب مش كامل. كمّل البيانات الأول.")

        # ✅ منع ازدواج الطلب
        if has_pending_request(user_id):
            return bot.answer_callback_query(call.id, f"⏳ {name}، عندك طلب جاري. نكمّله وبعدين ابعت الجديد.")

        product   = order["product"]
        player_id = order["player_id"]
        price_syp = convert_price_usd_to_syp(product.price)

        # المنتج ما زال فعّال؟
        if not is_product_active(product.product_id):
            return bot.answer_callback_query(call.id, f"⛔ {name}، المنتج متوقّف مؤقتًا.")

        # تحقق الرصيد (المتاح فقط)
        available = get_available_balance(user_id)
        if available < price_syp:
            bot.send_message(
                user_id,
                f"❌ {name}، رصيدك المتاح مش مكفّي.\n"
                f"المتاح: {_fmt_syp(available)}\n"
                f"السعر: {_fmt_syp(price_syp)}\n"
                f"🧾 اشحن المحفظة وبعدين جرّب تاني."
            )
            return

        # ✅ حجز المبلغ فعليًا عبر RPC
        hold_id = None
        try:
            resp = create_hold(user_id, price_syp)  # TTL الافتراضي
            if getattr(resp, "error", None):
                err_msg = str(resp.error).lower()
                if "insufficient_funds" in err_msg or "amount must be > 0" in err_msg:
                    bot.send_message(
                        user_id,
                        f"❌ {name}، الرصيد مش كفاية للحجز.\n"
                        f"المتاح: {_fmt_syp(available)}\n"
                        f"السعر: {_fmt_syp(price_syp)}"
                    )
                    return
                logging.error("create_hold RPC error: %s", resp.error)
                bot.send_message(user_id, f"❌ يا {name}، حصل خطأ بسيط أثناء الحجز. جرّب كمان شوية.")
                return
            hold_id = resp.data  # UUID
            if not hold_id:
                bot.send_message(user_id, f"❌ يا {name}، مش قادرين ننشئ الحجز دلوقتي. حاول تاني.")
                return
        except Exception as e:
            logging.exception("create_hold exception: %s", e)
            bot.send_message(user_id, f"❌ يا {name}، حصلت مشكلة أثناء الحجز. حاول بعد شوية.")
            return

        # عرض الرصيد الحالي في رسالة الأدمن
        balance = get_balance(user_id)

        admin_msg = (
            f"💰 رصيد المستخدم: {balance:,} ل.س\n"
            f"🆕 طلب جديد\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"آيدي اللاعب: <code>{player_id}</code>\n"
            f"🔖 المنتج: {product.name}\n"
            f"التصنيف: {product.category}\n"
            f"💵 السعر: {price_syp:,} ل.س\n"
            f"(select_{product.product_id})"
        )

        # تمرير hold_id للإدارة لإتمام/إلغاء لاحقًا + معلومة reserved
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "order",
                "product_id": product.product_id,
                "player_id": player_id,
                "price": price_syp,
                "reserved": price_syp,
                "hold_id": hold_id
            }
        )

        # رسالة موحّدة للعميل بعد إرسال الطلب
        bot.send_message(
            user_id,
            f"✅ تمام يا {name}! بعتنا طلبك للإدارة.\n"
            f"⏱️ سيتم تنفيذ الطلب {ETA_TEXT}.\n"
            f"ℹ️ لحد ما نخلّص الطلب ده، مش هتقدر تبعت طلب جديد.\n"
            f"📦 تفاصيل سريعة: حجزنا {_fmt_syp(price_syp)} لطلب «{product.name}» لآيدي اللاعب «{player_id}».",
        )
        process_queue(bot)

# ================= نقطة التسجيل من main.py =================

def register(bot, history, admin_ids=None):
    global _MAINTENANCE_NOTICE
    _MAINTENANCE_NOTICE = True
    register_message_handlers(bot, history)
    setup_inline_handlers(bot, admin_ids=admin_ids or [])
