import os
import sys
import logging
import telebot
from config import API_TOKEN

import threading
import http.server
import socketserver

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")
user_state = {}

# ----------- الاستيرادات الصحيحة: -----------
from handlers import admin, bill_and_units, products, wallet
from services import wallet_service, queue_service

# ----------- تسجيل الهاندلرز (حسب الحاجة): -----------
admin.register(bot, user_state)
bill_and_units.register(bot)         # فقط (bot) إذا كانت الدالة لا تحتاج user_state
products.register(bot, user_state)
wallet.register(bot, user_state)


PORT = 8081

def run_dummy_server():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"🔌 Dummy server listening on port {PORT}")
        httpd.serve_forever()

# شغل الخادم في ثريد منفصل حتى لا يوقف البوت الأساسي
threading.Thread(target=run_dummy_server, daemon=True).start()
# ===============================================================

# ---------------------------------------------------------
# تسجيل الأخطاء لظهورها في سجلّ Render
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

def _unhandled_exception_hook(exc_type, exc_value, exc_tb):
    """طباعة أي استثناء غير مُعالج بالكامل في اللوجز."""
    logging.critical("❌ Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _unhandled_exception_hook

# ---------------------------------------------------------
# ✅ فحص صحة API_TOKEN وجلب هوية البوت قبل التشغيل
# ---------------------------------------------------------
def check_api_token(token):
    try:
        # تمّ التصحيح: استخدم المتغير token بدلًا من API_TOKEN الثابت
        test_bot = telebot.TeleBot(token)
        me = test_bot.get_me()
        print(f"✅ التوكن سليم. هوية البوت: @{me.username} (ID: {me.id})")
        return True
    except Exception as e:
        logging.critical(f"❌ التوكن غير صالح أو لا يمكن الاتصال بـ Telegram API: {e}")
        sys.exit(1)

if not check_api_token(API_TOKEN):
    sys.exit(1)

# ---------------------------------------------------------
# 1) إنشاء كائن البوت ثم حذف أي Webhook سابق لتجنّب خطأ 409
# ---------------------------------------------------------
bot = telebot.TeleBot(API_TOKEN)
try:
    bot.delete_webhook(drop_pending_updates=True)
except Exception as e:
    logging.warning(f"⚠️ لم يتم حذف Webhook بنجاح: {e}")

# ---------------------------------------------------------
# 2) استيراد جميع الهاندلرز بعد تهيئة البوت
# ---------------------------------------------------------
from handlers import (
    start,
    wallet,
    support,
    admin,
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
    transfers_menu,      # أضفناها هنا للاستخدام
)

# ---------------------------------------------------------
# 3) حالة المستخدم
# ---------------------------------------------------------
user_state: dict[int, str] = {}
history: dict[int, list] = {}

# ---------------------------------------------------------
# 4) تسجيل جميع الهاندلرز (بدون تغيير أي شيء في القائمة الرئيسية)
# ---------------------------------------------------------
start.register(bot, user_state)
wallet.register(bot, history)
support.register(bot, user_state)
admin.register(bot, user_state)
recharge.register(bot, user_state)
cash_transfer.register(bot, history)
companies_transfer.register_companies_transfer(bot, history)
bill_and_units.register(bot)
products.register(bot, user_state)
media_services.register(bot, user_state)
wholesale.register(bot, user_state)
university_fees.register_university_fees(bot, history)
internet_providers.register(bot)

# ---------------------------------------------------------
# 4.1) ربط النظام الجديد لأوامر المنتجات (لا تحذف هذا السطر)
# ---------------------------------------------------------
ADMIN_IDS = [6935846121]
products.setup_inline_handlers(bot, ADMIN_IDS)

# ---------------------------------------------------------
# 5) زر الرجوع الذكي (ابقِه كما هو بدون تعديل)
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
# 6) ربط أزرار المنتجات بالخدمات الخاصة بها
# ---------------------------------------------------------
@bot.message_handler(func=lambda msg: msg.text == "تحويلات كاش و حوالات")
def handle_transfers(msg):
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

# ================== أزرار الشركات الجديدة ======================
@bot.message_handler(func=lambda msg: msg.text == "شركة الهرم")
def handle_al_haram(msg):
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة الهرم**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            "✔️ تأكيد حوالة الهرم", "❌ إلغاء"
        )
    )
    user_state[msg.from_user.id] = "alharam_start"

@bot.message_handler(func=lambda msg: msg.text == "شركة الفؤاد")
def handle_alfouad(msg):
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة الفؤاد**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            "✔️ تأكيد حوالة الفؤاد", "❌ إلغاء"
        )
    )
    user_state[msg.from_user.id] = "alfouad_start"

@bot.message_handler(func=lambda msg: msg.text == "شركة شخاشير")
def handle_shakhashir(msg):
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة شخاشير**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            "✔️ تأكيد حوالة شخاشير", "❌ إلغاء"
        )
    )
    user_state[msg.from_user.id] = "shakhashir_start"

# ---------------------------------------------------------
# === تشغيل نظام الطابور (QUEUE) ===
# ---------------------------------------------------------
from services.queue_service import process_queue
threading.Thread(target=process_queue, args=(bot,), daemon=True).start()

# ---------------------------------------------------------
# 7) تشغيل البوت مع نظام إعادة المحاولة والتنبيه في حال الخطأ
# ---------------------------------------------------------
import time

def restart_bot():
    """إعادة تشغيل البوت بعد حدوث خطأ قاتل."""
    logging.warning("🔄 إعادة تشغيل البوت بعد 10 ثوانٍ…")
    time.sleep(10)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def start_polling():
    print("🤖 البوت يعمل الآن…")
    while True:
        try:
            bot.infinity_polling(
                none_stop=True,
                skip_pending=True,
                long_polling_timeout=40,
            )
        except telebot.apihelper.ApiTelegramException as e:
            if getattr(e, "error_code", None) == 409:
                logging.critical("❌ تم إيقاف هذه النسخة لأن نسخة أخرى من البوت متصلة بالفعل.")
                break
            else:
                logging.error(f"🚨 خطأ في Telegram API: {e}")
                time.sleep(5)
                continue
        except Exception as e:
            logging.critical(f"❌ خطأ غير متوقع، سيُعاد تشغيل البوت: {e}")
            restart_bot()
            break

start_polling()

import scheduled_tasks  # لإطلاق المهام الدورية تلقائيًا عند تشغيل البوت

# ---------------------------------------------------------
# (تنبيه حول الضغط العالي – فكرة للطوابير/queues)
# ---------------------------------------------------------
# إذا لاحظت بطء أو سقوط البوت عند ضغط شديد،
# يمكنك استخدام مكتبة كطوابير الرسائل مثل queue.Queue أو celery
# لفصل استقبال الرسائل عن معالجتها في عملية مستقلة.
# ذلك متقدم جداً ويحتاج إعداد خادم خلفي غالباً.
# ---------------------------------------------------------
