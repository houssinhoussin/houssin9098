# main.py
import os
import sys
import time
import logging
import threading
import http.server
import socketserver
import telebot
from telebot import types

from config import (
    API_TOKEN,
    TELEGRAM_PARSE_MODE,
    LONG_POLLING_TIMEOUT,
    IS_WEBHOOK,
    WEBHOOK_URL,
)

# ---------------------------------------------------------
# لوج عام
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

def _unhandled_exception_hook(exc_type, exc_value, exc_tb):
    logging.critical("❌ Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))
sys.excepthook = _unhandled_exception_hook

# ---------------------------------------------------------
# إنشاء البوت + فحص التوكن
# ---------------------------------------------------------
def check_api_token(token: str) -> None:
    try:
        test_bot = telebot.TeleBot(token)
        me = test_bot.get_me()
        logging.info(f"✅ التوكن سليم. هوية البوت: @{me.username} (ID: {me.id})")
    except Exception as e:
        logging.critical(f"❌ التوكن غير صالح أو لا يمكن الاتصال بـ Telegram API: {e}")
        sys.exit(1)

check_api_token(API_TOKEN)

bot = telebot.TeleBot(API_TOKEN, parse_mode=TELEGRAM_PARSE_MODE)

# نحذف أي ويبهوك سابق حتى لا يحدث 409 عند الـ polling
try:
    bot.delete_webhook(drop_pending_updates=True)
except Exception as e:
    logging.warning(f"⚠️ لم يتم حذف Webhook بنجاح: {e}")

if IS_WEBHOOK:
    logging.warning(
        "⚠️ تم ضبط WEBHOOK_URL في .env، لكن هذا التطبيق يعمل بنمط Polling. "
        "سيتم تجاهل الويبهوك واستخدام polling."
    )

# ---------------------------------------------------------
# استيراد الهاندلرز والخدمات (مرّة واحدة)
# ---------------------------------------------------------
from handlers import (
    start,
    wallet,
    support,
    admin,
    ads,
    recharge,
    cash_transfer,
    companies_transfer,
    products,
    media_services,
    wholesale,
    university_fees,
    internet_providers,
    bill_and_units,
)
from handlers.keyboards import (
    main_menu,
    products_menu,
    game_categories,
    recharge_menu,
    companies_transfer_menu,
    cash_transfer_menu,
    syrian_balance_menu,
    wallet_menu,
    support_menu,
    links_menu,
    media_services_menu,
    transfers_menu,
)
from services.queue_service import process_queue
from services.scheduled_tasks import post_ads_task

# ---------------------------------------------------------
# حالة/تاريخ (لبعض الهاندلرز التي ما زالت تحتاجهما)
# ---------------------------------------------------------
user_state: dict[int, str] = {}
history: dict[int, list] = {}

# ---------------------------------------------------------
# تسجيل جميع الهاندلرز (بدون تكرار)
# ---------------------------------------------------------
start.register(bot, user_state)
wallet.register(bot, history)
support.register(bot, user_state)
admin.register(bot, user_state)
ads.register(bot, user_state)
recharge.register(bot, user_state)
cash_transfer.register(bot, history)
companies_transfer.register_companies_transfer(bot, history)
bill_and_units.register_bill_and_units(bot, user_state)
products.register(bot, user_state)
media_services.register(bot, user_state)
wholesale.register(bot, user_state)
university_fees.register_university_fees(bot, history)
internet_providers.register(bot)

# ربط نظام أزرار المنتجات (حسب كودك)
ADMIN_IDS = [int(os.getenv("ADMIN_MAIN_ID", "0"))] if os.getenv("ADMIN_MAIN_ID") else []
try:
    products.setup_inline_handlers(bot, ADMIN_IDS)
except Exception as e:
    logging.warning(f"⚠️ setup_inline_handlers(products) فشل: {e}")

# ---------------------------------------------------------
# إشعار قناة (اختياري/مُعطّل مثل كودك)
# ---------------------------------------------------------
def notify_channel_on_start(_bot):
    # مُعطّل بحسب كودك الأصلي
    pass

notify_channel_on_start(bot)

# ---------------------------------------------------------
# خادم صحي بسيط (حتى تبقى الخدمة حيّة على Render)
# يستخدم المنفذ من البيئة (Render يمرر PORT) وإلا 8081
# ---------------------------------------------------------
PORT = int(os.getenv("PORT", "8081"))

def run_dummy_server():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        logging.info(f"🔌 Health server listening on port {PORT}")
        httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# ---------------------------------------------------------
# ثريدات خلفية: الطابور + مجدول الإعلانات
# ---------------------------------------------------------
threading.Thread(target=process_queue, args=(bot,), daemon=True).start()
threading.Thread(target=post_ads_task, args=(bot,), daemon=True).start()

# ---------------------------------------------------------
# زر الرجوع الذكي (بدون تعديل على أوامرك)
# ---------------------------------------------------------
@bot.message_handler(func=lambda msg: msg.text == "⬅️ رجوع")
def handle_back(msg):
    user_id = msg.from_user.id
    state = user_state.get(user_id, "main_menu")

    if state == "products_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى المنتجات.", reply_markup=products_menu())
        user_state[user_id] = "main_menu"
    elif state == "main_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى القائمة الرئيسية.", reply_markup=main_menu())
    elif state == "game_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى الألعاب.", reply_markup=game_categories())
        user_state[user_id] = "products_menu"
    elif state == "cash_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى قائمة الكاش.", reply_markup=cash_transfer_menu())
        user_state[user_id] = "main_menu"
    elif state == "syrian_transfer":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى تحويل الرصيد السوري.", reply_markup=syrian_balance_menu())
        user_state[user_id] = "products_menu"
    else:
        bot.send_message(msg.chat.id, "⬅️ عدت إلى البداية.", reply_markup=main_menu())
        user_state[user_id] = "main_menu"

# ---------------------------------------------------------
# ربط أزرار رئيسية بخدماتها (كما في كودك)
# ---------------------------------------------------------
@bot.message_handler(func=lambda msg: msg.text == "تحويلات كاش و حوالات")
def handle_transfers(msg):
    bot.send_message(
        msg.chat.id,
        "من خلال هذه الخدمة تستطيع تحويل رصيد محفظتك إليك أو لأي شخص آخر عن طريق شركات الحوالات (كالهرم)، أو كرصيد كاش (سيرياتيل/MTN)."
    )
    bot.send_message(msg.chat.id, "اختر نوع التحويل:", reply_markup=transfers_menu())
    user_state[msg.from_user.id] = "transfers_menu"

@bot.message_handler(func=lambda msg: msg.text == "💵 تحويل الى رصيد كاش")
def handle_cash_transfer(msg):
    from handlers.cash_transfer import start_cash_transfer
    start_cash_transfer(bot, msg, history)

@bot.message_handler(func=lambda msg: msg.text == "حوالة مالية عبر شركات")
def handle_companies_transfer(msg):
    from handlers.companies_transfer import register_companies_transfer
    register_companies_transfer(bot, history)

@bot.message_handler(func=lambda msg: msg.text == "💳 تحويل رصيد سوري")
def handle_syrian_units(msg):
    from handlers.syr_units import start_syriatel_menu
    start_syriatel_menu(bot, msg)

@bot.message_handler(func=lambda msg: msg.text == "🌐 دفع مزودات الإنترنت ADSL")
def handle_internet(msg):
    from handlers.internet_providers import start_internet_provider_menu
    start_internet_provider_menu(bot, msg)

@bot.message_handler(func=lambda msg: msg.text == "🎓 دفع رسوم جامعية")
def handle_university_fees(msg):
    from handlers.university_fees import start_university_fee
    start_university_fee(bot, msg)

@bot.message_handler(func=lambda msg: msg.text in [
    "🖼️ تصميم لوغو احترافي",
    "📱 إدارة ونشر يومي",
    "📢 إطلاق حملة إعلانية",
    "🧾 باقة متكاملة شهرية",
    "✏️ طلب مخصص"
])
def handle_media(msg):
    from handlers.media_services import show_media_services
    show_media_services(bot, msg, user_state)

# شركات الحوالات (حسب نصوصك)
@bot.message_handler(func=lambda msg: msg.text == "شركة الهرم")
def handle_al_haram(msg):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
        "✔️ تأكيد حوالة الهرم", "❌ إلغاء"
    )
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة الهرم**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=kb
    )
    user_state[msg.from_user.id] = "alharam_start"

@bot.message_handler(func=lambda msg: msg.text == "شركة الفؤاد")
def handle_alfouad(msg):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
        "✔️ تأكيد حوالة الفؤاد", "❌ إلغاء"
    )
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة الفؤاد**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=kb
    )
    user_state[msg.from_user.id] = "alfouad_start"

@bot.message_handler(func=lambda msg: msg.text == "شركة شخاشير")
def handle_shakhashir(msg):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
        "✔️ تأكيد حوالة شخاشير", "❌ إلغاء"
    )
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة شخاشير**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=kb
    )
    user_state[msg.from_user.id] = "shakhashir_start"

# ---------------------------------------------------------
# بدء البولّينغ مع إعادة محاولة
# ---------------------------------------------------------
def start_polling():
    logging.info("🤖 البوت يعمل الآن… (Long Polling)")
    while True:
        try:
            bot.infinity_polling(
                none_stop=True,
                skip_pending=True,
                long_polling_timeout=LONG_POLLING_TIMEOUT,
            )
        except telebot.apihelper.ApiTelegramException as e:
            if getattr(e, "error_code", None) == 409:
                logging.critical("❌ تم إيقاف هذه النسخة لأن نسخة أخرى من البوت متصلة بالفعل.")
                break
            logging.error(f"🚨 خطأ في Telegram API: {e}")
            time.sleep(5)
        except Exception as e:
            logging.critical(f"❌ خطأ غير متوقع، سيُعاد التشغيل: {e}", exc_info=True)
            time.sleep(10)

if __name__ == "__main__":
    start_polling()
