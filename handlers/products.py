import logging
from database.db import get_table
from telebot import types
from services.wallet_service import register_user_if_not_exist, get_balance, deduct_balance
from config import BOT_NAME
from handlers import keyboards
from services.queue_service import process_queue, add_pending_request
from database.models.product import Product
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
PRODUCTS = {
    "PUBG": [
        Product(1, "60 شدة", "ألعاب", 0.89, "زر 60 شدة"),
        Product(2, "325 شدة", "ألعاب", 4.44, "زر 325 شدة"),
        Product(3, "660 شدة", "ألعاب", 8.85, "زر 660 شدة"),
        Product(4, "1800 شدة", "ألعاب", 22.09, "زر 1800 شدة"),
        Product(5, "3850 شدة", "ألعاب", 43.24, "زر 3850 شدة"),
        Product(6, "8100 شدة", "ألعاب", 86.31, "زر 8100 شدة"),
    ],
    "FreeFire": [
        Product(7, "100 جوهرة", "ألعاب", 0.98, "زر 100 جوهرة"),
        Product(8, "310 جوهرة", "ألعاب", 2.49, "زر 310 جوهرة"),
        Product(9, "520 جوهرة", "ألعاب", 4.13, "زر 520 جوهرة"),
        Product(10, "1060 جوهرة", "ألعاب", 9.42, "زر 1060 جوهرة"),
        Product(11, "2180 جوهرة", "ألعاب", 18.84, "زر 2180 جوهرة"),
    ],
    "Jawaker": [
        Product(12, "10000 توكنز", "ألعاب", 1.34, "زر 10000 توكنز"),
        Product(13, "15000 توكنز", "ألعاب", 2.01, "زر 15000 توكنز"),
        Product(14, "20000 توكنز", "ألعاب", 2.68, "زر 20000 توكنز"),
        Product(15, "30000 توكنز", "ألعاب", 4.02, "زر 30000 توكنز"),
        Product(16, "60000 توكنز", "ألعاب", 8.04, "زر 60000 توكنز"),
        Product(17, "120000 توكنز", "ألعاب", 16.08, "زر 120000 توكنز"),
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

def show_products_menu(bot, message):
    bot.send_message(message.chat.id, "📍 اختر نوع المنتج:", reply_markup=keyboards.products_menu())

def show_game_categories(bot, message):
    bot.send_message(message.chat.id, "🎮 اختر اللعبة أو التطبيق:", reply_markup=keyboards.game_categories())

def show_product_options(bot, message, category):
    options = PRODUCTS.get(category, [])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for p in options:
        keyboard.add(types.InlineKeyboardButton(f"{p.name} ({p.price}$)", callback_data=f"select_{p.product_id}"))
    keyboard.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_categories"))
    bot.send_message(message.chat.id, f"📦 اختر الكمية لـ {category}:", reply_markup=keyboard)

def handle_player_id(message, bot):
    user_id = message.from_user.id
    player_id = message.text.strip()

    order = user_orders.get(user_id)
    if not order or "product" not in order:
        bot.send_message(user_id, "❌ لم يتم تحديد طلب صالح.")
        return

    order["player_id"] = player_id
    product = order["product"]
    price_syp = convert_price_usd_to_syp(product.price)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("✅ تأكيد الطلب", callback_data="final_confirm_order"),
        types.InlineKeyboardButton("✏️ تعديل الآيدي", callback_data="edit_player_id"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_order")
    )


    bot.send_message(
        user_id,
        (
            f"هل أنت متأكد من شراء {product.name}؟\n"
            f"تفاصيل المنتج:\n"
            f"• اسم الزر: {getattr(product, 'button_name', '---')}\n"
            f"• التصنيف: {product.category}\n"
            f"• السعر: {price_syp:,} ل.س\n"
            f"• الآيدي: <code>{player_id}</code>\n"
            f"سيتم إرسال طلبك للإدارة وسَيُخصم المبلغ فقط عند موافقة الإدارة.\n"
            f"بعد التأكيد لن تتمكن من إرسال طلب آخر حتى إنهاء الطلب الحالي."
        ),
        reply_markup=keyboard
    )

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
        if has_pending_request(user_id):
            bot.send_message(
                msg.chat.id,
                "⚠️ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )
            return
        if has_pending_request(user_id):
            bot.send_message(
                msg.chat.id,
                "⚠️ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )
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

def setup_inline_handlers(bot, admin_ids):
    @bot.callback_query_handler(func=lambda c: c.data.startswith("select_"))
    def on_select_product(call):
        user_id = call.from_user.id
        if has_pending_request(user_id):
            bot.send_message(
                call.message.chat.id,
                "⚠️ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )
            return

        product_id = int(call.data.split("_", 1)[1])
        selected = None
        for items in PRODUCTS.values():
            for p in items:
                if p.product_id == product_id:
                    selected = p
                    break
            if selected:
                break
        if not selected:
            bot.answer_callback_query(call.id, "❌ المنتج غير موجود.")
            return
        user_orders[user_id] = {"category": selected.category, "product": selected}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_products"))
        msg = bot.send_message(user_id, "💡 أدخل آيدي اللاعب الخاص بك:", reply_markup=kb)
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
        user_orders.pop(user_id, None)
        bot.send_message(user_id, "❌ تم إلغاء الطلب.", reply_markup=keyboards.products_menu())

    @bot.callback_query_handler(func=lambda c: c.data == "final_confirm_order")
    def final_confirm_order(call):
        user_id = call.from_user.id
        if has_pending_request(user_id):
            bot.send_message(
                call.message.chat.id,
                "⚠️ لديك طلب قيد الانتظار، الرجاء الانتظار حتى تتم معالجته."
            )
            return
        order = user_orders.get(user_id)
        if not order or "product" not in order or "player_id" not in order:
            bot.answer_callback_query(call.id, "❌ لم يتم تجهيز الطلب بالكامل.")
            return
        product = order["product"]
        player_id = order["player_id"]
        price_syp = convert_price_usd_to_syp(product.price)

        # **تحقق من الرصيد قبل إرسال الطلب للإدمن والطابور**
        balance = get_balance(user_id)
        if balance < price_syp:
            bot.send_message(
                user_id,
                f"❌ لا يوجد رصيد كافٍ لإرسال الطلب.\nرصيدك الحالي: {balance:,} ل.س\nالسعر المطلوب: {price_syp:,} ل.س\nيرجى شحن المحفظة أولاً."
            )
            return
        # حجز المبلغ فور إرسال الطلب للطابور
        deduct_balance(user_id, price_syp)
        # تحديث الرصيد اللحظي بعد الحجز
        balance = get_balance(user_id)

        admin_msg = (
            f"💰 رصيد المستخدم: {balance:,} ل.س\n"

            f"🆕 طلب جديد\n"
            f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"آيدي اللاعب: <code>{player_id}</code>\n"
            f"🔖 المنتج: {product.name}\n"
            f"زر المنتج: <code>{getattr(product, 'button_name', '---')}</code>\n"
            f"التصنيف: {product.category}\n"
            f"💵 السعر: {price_syp:,} ل.س\n"
            f"(select_{product.product_id})"
        )
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "order",
                "product_id": product.product_id,
                "player_id": player_id,
                "price": price_syp,
                "reserved": price_syp
            }
        )  # ← هنا نغلق القوسين

        bot.send_message(
            user_id,
            "✅ تم إرسال طلبك للإدارة. سيتم معالجته خلال مدة من 1 إلى 4 دقائق. لن تتمكن من تقديم طلب جديد حتى معالجة هذا الطلب."
        )
        process_queue(bot)   # ← هذا السطر مهم جداً!
    # أضف الكولباك هنا
    @bot.callback_query_handler(func=lambda c: c.data == "edit_player_id")
    def edit_player_id(call):
        user_id = call.from_user.id
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_products"))
        msg = bot.send_message(user_id, "📋 يرجى إدخال آيدي اللاعب الجديد:", reply_markup=kb)
        bot.register_next_step_handler(msg, handle_player_id, bot)

def register(bot, history):
    # تسجيل الهاندلرات للرسائل (استدعاء دالة خاصة بذلك)
    register_message_handlers(bot, history)
    # تسجيل الهاندلرات للكولباك
    setup_inline_handlers(bot, admin_ids=[])
