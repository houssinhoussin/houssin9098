# handlers/products.py                                                                                      # handlers/products.py

from services.products_admin import get_product_active
import logging
import math
from database.db import get_table
from telebot import types
from services.system_service import is_maintenance, maintenance_message
from services.wallet_service import (
    register_user_if_not_exist,
    get_balance,
    get_available_balance,
    create_hold,   # ✅ حجز ذرّي
)
from config import BOT_NAME
from handlers import keyboards
from services.queue_service import process_queue, add_pending_request
from database.models.product import Product

# (جديد) فلاغات المزايا للمنتجات الفردية
from services.feature_flags import is_feature_enabled  # نستخدمه لتعطيل منتج معيّن (مثل 660 شدة)
from services.feature_flags import UNAVAILABLE_MSG

# حارس التأكيد الموحّد: يحذف الكيبورد + يعمل Debounce
try:
    from services.ui_guards import confirm_guard
except Exception:
    from ui_guards import confirm_guard

# ==== Helpers للرسائل الموحدة ====
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."
ETA_TEXT = "من 1 إلى 4 دقائق"
PAGE_SIZE_PRODUCTS = 7  # ✅ عرض كل المنتجات بالصفحات بدلاً من ظهور 3 فقط

# يحذف كيبورد الرسالة الحالية (inline) بدون حذف النص
def _hide_inline_kb(bot, call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

def _name_from_user(u) -> str:
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _with_cancel(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _card(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def _unavailable_short(product_name: str) -> str:
    return UNAVAILABLE_MSG.format(label=product_name)
    
# ===== تصنيف مرئي واضح للرسائل (حسب الطلبية) =====
_CATEGORY_LABELS = {
    "PUBG": "شحن شدات ببجي",
    "FreeFire": "شحن جواهر فري فاير",
    "Jawaker": "تطبيق جواكر",
}
_MIXED_SUB_LABELS = {
    "cod": "كول أوف ديوتي",
    "bigo": "بيغو لايف",
}

def _visible_category_label(order: dict, product: Product) -> str:
    """يرجع اسم الفئة المفهوم للمستخدم/الأدمن بدل 'ألعاب/تطبيقات'."""
    cat = (order or {}).get("category") or getattr(product, "category", "") or ""

    # MixedApps: نحدده من subset، أو من الوسم داخل الوصف (app:cod/app:bigo)
    if cat == "MixedApps":
        key = ((order or {}).get("subset") or "").strip().lower()

        if not key:
            # fallback: استخرج من الوصف/أي حقل نصي يحمل app:...
            desc_all = ""
            for attr in ("description", "desc", "label", "button", "button_label", "extra"):
                v = getattr(product, attr, None)
                if isinstance(v, str) and v:
                    desc_all = v
                    break
            if not desc_all:
                try:
                    for v in getattr(product, "__dict__", {}).values():
                        if isinstance(v, str) and "app:" in v:
                            desc_all = v
                            break
                except Exception:
                    pass
            d = (desc_all or "").lower()
            if "app:cod" in d:
                key = "cod"
            elif "app:bigo" in d:
                key = "bigo"

        return _MIXED_SUB_LABELS.get(key, "ألعاب/تطبيقات")

    # غير MixedApps
    return _CATEGORY_LABELS.get(cat, cat)

# ================= (جديد) تحكّم تفصيلي ON/OFF لكل زر كمية =================
# نستخدم جدول features نفسه بمفاتيح منسّقة لكل خيار (SKU)
_FEATURES_TABLE = "features"

def _features_tbl():
    return get_table(_FEATURES_TABLE)

def _slug(s: str) -> str:
    return (s or "").strip().replace(" ", "-").replace("ـ", "-").lower()

def key_product_option(category: str, product_name: str) -> str:
    # مثال: product:pubg:60-شدة  /  product:freefire:310-جوهرة
    return f"product:{_slug(category)}:{_slug(product_name)}"

def ensure_feature(key: str, label: str, default_active: bool = True) -> None:
    """يزرع السطر في features إن لم يوجد (idempotent)، ويحدّث label إن تغيّر."""
    try:
        r = _features_tbl().select("key").eq("key", key).limit(1).execute()
        if not getattr(r, "data", None):
            _features_tbl().insert({"key": key, "label": label, "active": bool(default_active)}).execute()
        else:
            _features_tbl().update({"label": label}).eq("key", key).execute()
    except Exception as e:
        logging.exception("[products] ensure_feature failed: %s", e)

def is_option_enabled(category: str, product_name: str, default: bool = True) -> bool:
    """يرجع حالة التفعيل لزر الكمية المحدّد."""
    try:
        return is_feature_enabled(key_product_option(category, product_name), default)
    except Exception:
        return default

def require_option_or_alert(bot, chat_id: int, category: str, product_name: str) -> bool:
    """إن كان الزر مقفول يرسل اعتذار ويرجع True (يعني قف)."""
    if is_option_enabled(category, product_name, True):
        return False
    try:
        bot.send_message(
            chat_id,
            _with_cancel(
                f"⛔ عذرًا، «{product_name}» غير متاح حاليًا (نفاد الكمية/صيانة).\n"
                f"نعمل على إعادته في أسرع وقت. شكرًا لتفهّمك 🤍"
            )
        )
    except Exception:
        pass
    return True

# حالة الطلبات لكل مستخدم (للخطوات فقط، مش منع تعدد الطلبات)
user_orders = {}

def has_pending_request(user_id: int) -> bool:
    """ترجع True إذا كان لدى المستخدم طلب قيد الانتظار (موجودة للتوافق؛ مش بنمنع تعدد الطلبات)."""
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
        Product(1, "60 شدة", "ألعاب", 0.87, "زر 60 شدة"),
        Product(2, "120 شدة", "ألعاب", 1.74, "زر 120 شدة"),
        Product(3, "180 شدة", "ألعاب", 2.61, "زر 180 شدة"),
        Product(4, "240 شدة", "ألعاب", 3.48, "زر 240 شدة"),
        Product(5, "325 شدة", "ألعاب", 4.44, "زر 325 شدة"),
        Product(6, "360 شدة", "ألعاب", 5.21, "زر 360 شدة"),
        Product(7, "505 شدة", "ألعاب", 6.95, "زر 505 شدة"),
        Product(8, "660 شدة", "ألعاب", 8.85, "زر 660 شدة"),
        Product(9, "840 شدة", "ألعاب", 11.31, "زر 840 شدة"),
        Product(10, "1800 شدة", "ألعاب", 22.09, "زر 1800 شدة"),
         Product(11, "2125 شدة", "ألعاب", 25.64, "زر 2125 شدة"),
        Product(12, "3850 شدة", "ألعاب", 43.24, "زر 3850 شدة"),
        Product(13, "8100 شدة", "ألعاب", 86.31, "زر 8100 شدة"),
    ],
    "FreeFire": [
        Product(14, "100 جوهرة", "ألعاب", 0.98, "زر 100 جوهرة"),
        Product(15, "310 جوهرة", "ألعاب", 2.49, "زر 310 جوهرة"),
        Product(16, "520 جوهرة", "ألعاب", 4.13, "زر 520 جوهرة"),
        Product(17, "1060 جوهرة", "ألعاب", 9.42, "زر 1060 جوهرة"),
        Product(18, "2180 جوهرة", "ألعاب", 18.84, "زر 2180 جوهرة"),
        Product(19, "عضوية أسبوع", "ألعاب", 3.60, "عضوية أسبوع  عضوية أسبوع"),
        Product(20, "عضوية شهر",  "ألعاب", 13.00, "عضوية شهر  عضوية شهر"),
    ],
    "Jawaker": [
        Product(21, "10000 توكنز", "ألعاب", 1.34, "زر 10000 توكنز"),
        Product(22, "15000 توكنز", "ألعاب", 2.01, "زر 15000 توكنز"),
        Product(23, "20000 توكنز", "ألعاب", 2.68, "زر 20000 توكنز"),
        Product(24, "30000 توكنز", "ألعاب", 4.02, "زر 30000 توكنز"),
        Product(25, "60000 توكنز", "ألعاب", 8.04, "زر 60000 توكنز"),
        Product(26, "120000 توكنز", "ألعاب", 16.08, "زر 120000 توكنز"),
    ],
    "MixedApps": [
        # === Call of Duty ===
        Product(27, "88 نقطة",   "ألعاب/تطبيقات", 1.28,  "app:cod|COD 88 CP"),
        Product(28, "460 نقطة",  "ألعاب/تطبيقات", 5.56,  "app:cod|COD 460 CP"),
        Product(29, "960 نقطة",  "ألعاب/تطبيقات", 9.56,  "app:cod|COD 960 CP"),
        Product(30, "2600 نقطة", "ألعاب/تطبيقات", 24.13, "app:cod|COD 2600 CP"),
        Product(31, "Battle Pass",         "ألعاب/تطبيقات", 3.08, "app:cod|COD Battle Pass"),
        Product(32, "Battle Pass Bundle",  "ألعاب/تطبيقات", 7.08, "app:cod|COD Battle Pass Bundle"),

        # === Bigo Live ===
        Product(33, "50 ألماس",    "ألعاب/تطبيقات", 0.94,  "app:bigo|Bigo Live 50 Diamonds"),
        Product(34, "100 ألماس",   "ألعاب/تطبيقات", 1.88,  "app:bigo|Bigo Live 100 Diamonds"),
        Product(35, "200 ألماس",   "ألعاب/تطبيقات", 3.64,  "app:bigo|Bigo Live 200 Diamonds"),
        Product(36, "400 ألماس",   "ألعاب/تطبيقات", 7.25,  "app:bigo|Bigo Live 400 Diamonds"),
        Product(37, "600 ألماس",   "ألعاب/تطبيقات", 10.86, "app:bigo|Bigo Live 600 Diamonds"),
        Product(38, "1000 ألماس",  "ألعاب/تطبيقات", 18.09, "app:bigo|Bigo Live 1000 Diamonds"),
        Product(39, "1500 ألماس",  "ألعاب/تطبيقات", 27.09, "app:bigo|Bigo Live 1500 Diamonds"),
        Product(40, "2000 ألماس",  "ألعاب/تطبيقات", 36.12, "app:bigo|Bigo Live 2000 Diamonds"),
        Product(41, "3000 ألماس",  "ألعاب/تطبيقات", 54.19, "app:bigo|Bigo Live 3000 Diamonds"),
        Product(42, "4000 ألماس",  "ألعاب/تطبيقات", 72.22, "app:bigo|Bigo Live 4000 Diamonds"),
    ],
}

# ================= (جديد) أقسام فرعية قابلة للتوسّع لقسم MixedApps =================
# لإضافة زر جديد لاحقًا يكفي إضافة dict جديد هنا بنفس البنية (label/key)
MIXEDAPPS_SUBCATS = [
    {"label": "Call of Duty", "key": "cod"},
    {"label": "Bigo Live",    "key": "bigo"},
]

def _filter_products_by_key(category: str, key_text: str) -> list[Product]:
    """يرجع باقات التصنيف بحسب وسم التطبيق في أي حقل نصي داخل الكائن (app:cod / app:bigo)."""
    options = PRODUCTS.get(category, [])
    k = (key_text or "").strip().lower()
    tag = f"app:{k}"

    result = []
    for p in options:
        desc = ""
        # جرّب أسماء حقول شائعة
        for attr in ("description", "desc", "label", "button", "button_label", "extra"):
            v = getattr(p, attr, None)
            if isinstance(v, str) and v:
                desc = v
                break
        # لو ما لقينا، دوّر بأي قيمة نصية داخل الكائن
        if not desc:
            try:
                for v in getattr(p, "__dict__", {}).values():
                    if isinstance(v, str) and "app:" in v:
                        desc = v
                        break
            except Exception:
                pass

        desc_l = (desc or "").lower()
        name_l = (p.name or "").lower()

        if tag in desc_l or tag in name_l:
            result.append(p)

    return result

def convert_price_usd_to_syp(usd):
    # ✅ تنفيذ شرطك: تحويل مرة واحدة + round() ثم int (بدون فواصل عشرية)
    if usd <= 5:
        return int(round(usd * 11800))
    elif usd <= 10:
        return int(round(usd * 11600))
    elif usd <= 20:
        return int(round(usd * 11300))
    return int(round(usd * 11000))

def _button_label(p: Product) -> str:
    try:
        return f"{(p.name or '').strip()} بسعر ${float(p.price):.2f}"
    except Exception:
        return f"{p.name}"

def _build_products_keyboard(category: str, page: int = 0):
    """لوحة منتجات مع صفحات + إبراز المنتجات الموقوفة + (جديد) فلاغ لكل كمية."""
    options = PRODUCTS.get(category, [])
    total = len(options)

    # 🌱 زرع مفاتيح features لكل زر كمية (تظهر عند الإدمن لإيقاف خيار محدد)
    for p in options:
        try:
            ensure_feature(
                key_product_option(category, p.name),
                f"{category} — {p.name}",
                default_active=True
            )
        except Exception:
            pass

    pages = max(1, math.ceil(total / PAGE_SIZE_PRODUCTS))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE_PRODUCTS
    end = start + PAGE_SIZE_PRODUCTS
    slice_items = options[start:end]

    kb = types.InlineKeyboardMarkup(row_width=2)

    for p in slice_items:
        # فعال على مستوى المنتج العام + فعال على مستوى هذا الخيار؟
        try:
            active_global = bool(get_product_active(p.product_id))
        except Exception:
            active_global = True

        active_option = is_option_enabled(category, p.name, True)
        active = active_global and active_option

        if active:
            # زر عادي لاختيار المنتج
            kb.add(types.InlineKeyboardButton(_button_label(p), callback_data=f"select_{p.product_id}"))
        else:
            # نعرضه لكن كموقوف — ويعطي Alert عند الضغط
            try:
                label = f"🔴 {p.name} — ${float(p.price):.2f} (موقوف)"
            except Exception:
                label = f"🔴 {p.name} (موقوف)"
            kb.add(types.InlineKeyboardButton(label, callback_data=f"prod_inactive:{p.product_id}"))

    # شريط تنقّل
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"prodpage:{category}:{page-1}"))
    nav.append(types.InlineKeyboardButton(f"{page+1}/{pages}", callback_data="prodnoop"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"prodpage:{category}:{page+1}"))
    if nav:
        kb.row(*nav)

    # أزرار مساعدة مختصرة
    kb.add(types.InlineKeyboardButton("💳 طرق الدفع/الشحن", callback_data="show_recharge_methods"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_categories"))
    return kb, pages

# ======== (جديد) باني لوحة لجزء فرعي (subset) داخل نفس التصنيف ========
def _build_products_keyboard_subset(category: str, options: list[Product], page: int = 0):
    """نسخة من الباني الرئيسي لكن تعمل على قائمة options المفلترة (مثل Call of Duty فقط داخل MixedApps)."""
    total = len(options)

    # 🌱 زرع مفاتيح features لكل زر كمية
    for p in options:
        try:
            ensure_feature(
                key_product_option(category, p.name),
                f"{category} — {p.name}",
                default_active=True
            )
        except Exception:
            pass

    pages = max(1, math.ceil(total / PAGE_SIZE_PRODUCTS))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE_PRODUCTS
    end = start + PAGE_SIZE_PRODUCTS
    slice_items = options[start:end]

    kb = types.InlineKeyboardMarkup(row_width=2)

    for p in slice_items:
        try:
            active_global = bool(get_product_active(p.product_id))
        except Exception:
            active_global = True

        active_option = is_option_enabled(category, p.name, True)
        active = active_global and active_option

        if active:
            kb.add(types.InlineKeyboardButton(_button_label(p), callback_data=f"select_{p.product_id}"))
        else:
            try:
                label = f"🔴 {p.name} — ${float(p.price):.2f} (موقوف)"
            except Exception:
                label = f"🔴 {p.name} (موقوف)"
            kb.add(types.InlineKeyboardButton(label, callback_data=f"prod_inactive:{p.product_id}"))

    # شريط تنقّل
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️", callback_data=f"prodpage:{category}:{page-1}"))
    nav.append(types.InlineKeyboardButton(f"{page+1}/{pages}", callback_data="prodnoop"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"prodpage:{category}:{page+1}"))
    if nav:
        kb.row(*nav)

    # أزرار مساعدة + رجوع
    kb.add(types.InlineKeyboardButton("💳 طرق الدفع/الشحن", callback_data="show_recharge_methods"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_categories"))
    return kb, pages

# ================= واجهات العرض =================

def show_products_menu(bot, message):
    name = _name_from_user(message.from_user)
    txt = _with_cancel(f"📍 أهلاً {name}! اختار نوع المنتج اللي يناسبك 😉")
    bot.send_message(message.chat.id, txt, reply_markup=keyboards.products_menu())

def show_game_categories(bot, message):
    name = _name_from_user(message.from_user)
    txt = _with_cancel(f"🎮 يا {name}، اختار اللعبة أو التطبيق اللي محتاجه:")
    bot.send_message(message.chat.id, txt, reply_markup=keyboards.game_categories())

def show_product_options(bot, message, category):
    # ⬅️ الآن مع صفحات + عرض كل المنتجات (حتى الموقوفة بعلامة 🔴)
    keyboard, pages = _build_products_keyboard(category, page=0)
    bot.send_message(
        message.chat.id,
        _with_cancel(f"📦 منتجات {category}: (صفحة 1/{pages}) — اختار اللي على مزاجك 😎"),
        reply_markup=keyboard
    )

# ================= خطوات إدخال آيدي اللاعب =================

def handle_player_id(message, bot):
    user_id = message.from_user.id
    player_id = (message.text or "").strip()
    name = _name_from_user(message.from_user)

    order = user_orders.get(user_id)
    if not order or "product" not in order:
        bot.send_message(user_id, f"❌ {name}، ما عندنا طلب شغّال دلوقتي. اختار المنتج وابدأ من جديد.")
        return

    product = order["product"]

    # 🔒 تحقّق سريع: قد يكون الإدمن أوقف خيار الكمية بعد ما اخترته
    if require_option_or_alert(bot, user_id, order.get("category", ""), product.name):
        return

    order["player_id"] = player_id
    price_syp = convert_price_usd_to_syp(product.price)

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("✅ تمام.. أكّد الطلب", callback_data="final_confirm_order"),
        types.InlineKeyboardButton("✏️ أعدّل الآيدي", callback_data="edit_player_id"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_order")
    )

    bot.send_message(
        user_id,
        _with_cancel(
            _card(
                "📦 تفاصيل الطلب",
                [
                    f"• المنتج: {product.name}",
                    f"• الفئة: {_visible_category_label(order, product)}",
                    f"• السعر: {_fmt_syp(price_syp)}",
                    f"• آيدي اللاعب: {player_id}",
                    "",
                    f"هنبعت الطلب للإدارة، والحجز هيتم فورًا. التنفيذ {ETA_TEXT} بإذن الله.",
                    "تقدر تعمل طلبات تانية برضه — بنحسب من المتاح بس."
                ]
            )
        ),
        reply_markup=keyboard
    )

# ================= تسجيل هاندلرات الرسائل =================

def register_message_handlers(bot, history):
    # /cancel — إلغاء سريع في أي خطوة
    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(msg):
        uid = msg.from_user.id
        user_orders.pop(uid, None)
        name = _name_from_user(msg.from_user)
        bot.send_message(
            msg.chat.id,
            _card("✅ تم الإلغاء", [f"يا {name}، رجعناك لقائمة المنتجات."]),
            reply_markup=keyboards.products_menu()
        )

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
        "🏏 تطبيق جواكر",
        "🎮 شحن العاب و تطبيقات مختلفة"
    ])
    def game_handler(msg):
        user_id = msg.from_user.id
        register_user_if_not_exist(user_id, msg.from_user.full_name)

        if is_maintenance():
            try:
                bot.send_message(msg.chat.id, maintenance_message())
            finally:
                return

        # ===== (جديد) لو كان الزر هو "🎮 شحن العاب و تطبيقات مختلفة" اعرض قائمة فرعية ديناميكية =====
        if msg.text in ("🎮 شحن العاب و تطبيقات مختلفة", "🎮 شحن ألعاب و تطبيقات مختلفة"):
            kb = types.InlineKeyboardMarkup(row_width=2)
            for sc in MIXEDAPPS_SUBCATS:
                kb.add(types.InlineKeyboardButton(sc["label"], callback_data=f"open_subcat:MixedApps:{sc['key']}"))
            kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_categories"))
            name = _name_from_user(msg.from_user)
            bot.send_message(
                msg.chat.id,
                _with_cancel(f"🎮 يا {name}، اختر اللعبة/التطبيق:"),
                reply_markup=kb
            )
            return  # لا نكمل للخريطة العامة

        category_map = {
            "🎯 شحن شدات ببجي العالمية": "PUBG",
            "🔥 شحن جواهر فري فاير": "FreeFire",
            "🏏 تطبيق جواكر": "Jawaker",
            "🎮 شحن العاب و تطبيقات مختلفة": "MixedApps",  # ✅ يبقى موجود لو احتجناه لاحقًا
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
        _hide_inline_kb(bot, call)

        # ابحث عن المنتج
        selected = None
        selected_category = None
        for cat, items in PRODUCTS.items():
            for p in items:
                if p.product_id == product_id:
                    selected = p
                    selected_category = cat
                    break
            if selected:
                break
        if not selected:
            return bot.answer_callback_query(call.id, f"❌ {name}، المنتج مش موجود. جرّب تاني.")

        # ✅ منع اختيار منتج موقوف (عامًا أو كخيار محدّد)
        if not get_product_active(product_id):
            return bot.answer_callback_query(call.id, _unavailable_short(selected.name), show_alert=True)
        if require_option_or_alert(bot, call.message.chat.id, selected_category or "", selected.name):
            return bot.answer_callback_query(call.id)

        user_orders[user_id] = {"category": selected_category or selected.category, "product": selected}
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_products"))
        msg = bot.send_message(user_id, _with_cancel(f"💡 يا {name}، ابعت آيدي اللاعب لو سمحت:"), reply_markup=kb)
        bot.register_next_step_handler(msg, handle_player_id, bot)
        bot.answer_callback_query(call.id)

    # ✅ (جديد) فتح تصنيف فرعي داخل MixedApps (Call of Duty / Bigo Live ...)
    @bot.callback_query_handler(func=lambda c: c.data.startswith("open_subcat:"))
    def _open_subcategory(call):
        user_id = call.from_user.id
        try:
            _, category, key_text = call.data.split(":", 2)  # مثال: open_subcat:MixedApps:Call of Duty
        except Exception:
            return bot.answer_callback_query(call.id)
        _hide_inline_kb(bot, call)

        # خزّن التصنيف + المفتاح (subset) للمستخدم عشان التنقل والرجوع
        user_orders[user_id] = {"category": category, "subset": key_text}

        # فلترة المنتجات داخل التصنيف بحسب المفتاح
        options = _filter_products_by_key(category, key_text)
        if not options:
            bot.answer_callback_query(call.id, "❌ لا توجد خيارات متاحة حاليًا.", show_alert=True)
            return

        kb, pages = _build_products_keyboard_subset(category, options, page=0)
        txt = _with_cancel(f"📦 منتجات {key_text}: (صفحة 1/{pages}) — اختار اللي على مزاجك 😎")

        try:
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, txt, reply_markup=kb)

        bot.answer_callback_query(call.id)

    # ✅ عرض صفحة جديدة من المنتجات
    @bot.callback_query_handler(func=lambda c: c.data.startswith("prodpage:"))
    def _paginate_products(call):
        try:
            _, category, page_str = call.data.split(":", 2)
            page = int(page_str)
        except Exception:
            return bot.answer_callback_query(call.id)
        _hide_inline_kb(bot, call)

        user_id = call.from_user.id
        order = user_orders.get(user_id, {})
        subset = order.get("subset")

        # إن كان المستخدم في subset داخل MixedApps، نحافظ على نفس الفلترة أثناء التنقل
        if subset and category == "MixedApps":
            options = _filter_products_by_key(category, subset)
            kb, pages = _build_products_keyboard_subset(category, options, page=page)
        else:
            kb, pages = _build_products_keyboard(category, page=page)

        try:
            bot.edit_message_text(
                _with_cancel(f"📦 منتجات {category}: (صفحة {page+1}/{pages}) — اختار اللي على مزاجك 😎"),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception:
            bot.send_message(
                call.message.chat.id,
                _with_cancel(f"📦 منتجات {category}: (صفحة {page+1}/{pages}) — اختار اللي على مزاجك 😎"),
                reply_markup=kb
            )
        bot.answer_callback_query(call.id)
        bot.answer_callback_query(call.id)  # ✅ يوقف المؤشّر الدوّار
    
    # ✅ ضغط على منتج موقوف — نعطي تنبيه فقط
    @bot.callback_query_handler(func=lambda c: c.data.startswith("prod_inactive:"))
    def _inactive_alert(call):
        pid = int(call.data.split(":", 1)[1])
        name = None
        for items in PRODUCTS.values():
            for p in items:
                if p.product_id == pid:
                    name = p.name
                break
            if name:
                break
        _hide_inline_kb(bot, call)  # ← أولًا
        bot.answer_callback_query(call.id, _unavailable_short(name or "المنتج"), show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data == "prodnoop")
    def _noop(call):
        _hide_inline_kb(bot, call)
        bot.answer_callback_query(call.id)


    @bot.callback_query_handler(func=lambda c: c.data == "show_recharge_methods")
    def _show_recharge(call):
        _hide_inline_kb(bot, call)
        # إن كانت recharge_menu ReplyKeyboardMarkup فهذا الطريق الصحيح:
        try:
            bot.send_message(call.message.chat.id, "💳 اختار طريقة شحن محفظتك:", reply_markup=keyboards.recharge_menu())
        except Exception:
            bot.send_message(call.message.chat.id, "💳 لعرض طرق الشحن، افتح قائمة الشحن من الرئيسية.")
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_products")
    def back_to_products(call):
        _hide_inline_kb(bot, call)
        user_id = call.from_user.id
        order = user_orders.get(user_id, {}) or {}
        category = order.get("category")
        subset = order.get("subset")

        if category:
            if subset and category == "MixedApps":
                options = _filter_products_by_key(category, subset)
                kb, pages = _build_products_keyboard_subset(category, options, page=0)
            else:
                kb, pages = _build_products_keyboard(category, page=0)
            try:
                bot.edit_message_text(
                    _with_cancel(f"📦 منتجات {category}: (صفحة 1/{pages}) — اختار اللي على مزاجك 😎"),
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=kb
                )
            except Exception:
                bot.send_message(
                    call.message.chat.id,
                    _with_cancel(f"📦 منتجات {category}: (صفحة 1/{pages}) — اختار اللي على مزاجك 😎"),
                    reply_markup=kb
                )

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_categories")
    def back_to_categories(call):
        _hide_inline_kb(bot, call)
        name = _name_from_user(call.from_user)
        txt = _with_cancel(f"🎮 يا {name}، اختار اللعبة أو التطبيق اللي محتاجه:")
        try:
            bot.edit_message_text(
                txt,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboards.game_categories()
            )
        except Exception:
            # fallback
            bot.send_message(call.message.chat.id, txt, reply_markup=keyboards.game_categories())
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == "cancel_order")
    def cancel_order(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        user_orders.pop(user_id, None)
        bot.send_message(user_id, f"❌ تم إلغاء الطلب يا {name}. بنجهّزلك عروض أحلى المرة الجاية 🤝", reply_markup=keyboards.products_menu())
        _hide_inline_kb(bot, call)
        bot.answer_callback_query(call.id)  # ✅ مهم لإيقاف الـ spinner
    @bot.callback_query_handler(func=lambda c: c.data == "edit_player_id")
    def edit_player_id(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="back_to_products"))
        msg = bot.send_message(user_id, _with_cancel(f"📋 يا {name}، ابعت آيدي اللاعب الجديد:"), reply_markup=kb)
        bot.register_next_step_handler(msg, handle_player_id, bot)
        _hide_inline_kb(bot, call)
        bot.answer_callback_query(call.id)  # ✅ يوقف المؤشّر الدوّار
        
    @bot.callback_query_handler(func=lambda c: c.data == "final_confirm_order")
    def final_confirm_order(call):
        user_id = call.from_user.id

        # ✅ احذف الكيبورد فقط + امنع الدبل-كليك (بدون حذف الرسالة)
        if confirm_guard(bot, call, "final_confirm_order"):
            return

        name = _name_from_user(call.from_user)
        order = user_orders.get(user_id)
        if not order or "product" not in order or "player_id" not in order:
            return bot.answer_callback_query(call.id, f"❌ {name}، الطلب مش كامل. كمّل البيانات الأول.")

        product   = order["product"]
        player_id = order["player_id"]
        price_syp = convert_price_usd_to_syp(product.price)

        # المنتج ما زال فعّال؟ (Alert برسالة احترافية)
        if not get_product_active(product.product_id):
            return bot.answer_callback_query(call.id, _unavailable_short(product.name), show_alert=True)

        # 🔒 الخيار نفسه ما زال مفعّل؟ (مثلاً: 660 شدة مقفلة)
        if require_option_or_alert(bot, call.message.chat.id, order.get("category", ""), product.name):
            return bot.answer_callback_query(call.id)

        # تحقق الرصيد (المتاح فقط)
        available = get_available_balance(user_id)
        if available < price_syp:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("💳 طرق الدفع/الشحن", callback_data="show_recharge_methods"))
            bot.send_message(
                user_id,
                _card(
                    "❌ رصيدك مش مكفّي",
                    [
                        f"المتاح: {_fmt_syp(available)}",
                        f"السعر: {_fmt_syp(price_syp)}",
                        "🧾 اشحن المحفظة وبعدين جرّب تاني."
                    ]
                ),
                reply_markup=kb
            )
            return

        # ✅ حجز المبلغ فعليًا (HOLD)
        hold_id = None
        try:
            resp = create_hold(user_id, price_syp, f"حجز شراء — {product.name} — آيدي {player_id}")
            if getattr(resp, "error", None):
                err_msg = str(resp.error).lower()
                if "insufficient_funds" in err_msg or "amount must be > 0" in err_msg:
                    bot.send_message(
                        user_id,
                        _card(
                            "❌ الرصيد غير كافٍ",
                            [f"المتاح: {_fmt_syp(available)}", f"السعر: {_fmt_syp(price_syp)}"]
                        )
                    )
                    return
                logging.error("create_hold RPC error: %s", resp.error)
                bot.send_message(user_id, f"❌ يا {name}، حصل خطأ بسيط أثناء الحجز. جرّب كمان شوية.")
            data = getattr(resp, "data", None)
            if isinstance(data, dict):
                hold_id = data.get("id") or data.get("hold_id")
            elif isinstance(data, (list, tuple)) and data:
                hold_id = data[0].get("id") if isinstance(data[0], dict) else data[0]
            else:
                hold_id = data
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
            f"التصنيف: {_visible_category_label(order, product)}\n"
            f"💵 السعر: {price_syp:,} ل.س\n"
            f"(select_{product.product_id})"
        )

        # ✅ تمرير hold_id + اسم المنتج الحقيقي داخل الـ payload
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "order",
                "product_id": product.product_id,
                "product_name": product.name,   # مهم لرسالة التنفيذ باسم المنتج
                "player_id": player_id,
                "price": price_syp,
                "reserved": price_syp,
                "hold_id": hold_id
            }
        )

        # رسالة موحّدة للعميل بعد إرسال الطلب
        bot.send_message(
            user_id,
            _with_cancel(
                _card(
                    f"✅ تمام يا {name}! طلبك اتبعت 🚀",
                    [
                        f"⏱️ التنفيذ {ETA_TEXT}.",
                        f"📦 حجزنا {_fmt_syp(price_syp)} لطلب «{product.name}» لآيدي «{player_id}».",
                        "تقدر تبعت طلبات تانية — بنسحب من المتاح بس."
                    ]
                )
            ),
        )
        process_queue(bot)

# ================= نقطة التسجيل من main.py =================

def register(bot, history, admin_ids=None):
    register_message_handlers(bot, history)
    setup_inline_handlers(bot, admin_ids=admin_ids or [])
