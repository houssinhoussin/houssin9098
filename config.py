import os
import logging

# إعداد تسجيل الأخطاء Logging (يُنصح أن يكون في أعلى الملف دائماً)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

# ✅ إعدادات البوت الأساسية
API_TOKEN = "7936418161:AAGNZEMIGZEmPfYlCGQbO_vM9oQbQUVSiT4"
BOT_USERNAME = "@my_fast_shop_bot"
BOT_NAME = "المتجر العالمي"
BOT_ID = 7936418161

# ✅ معلومات الأدمن الرئيسي (حسين)
ADMIN_MAIN_ID = 6935846121
ADMIN_MAIN_USERNAME = "@Houssin363"

# ✅ قناة الاشتراك الإجباري
FORCE_SUB_CHANNEL_ID = -1002852510917
FORCE_SUB_CHANNEL_USERNAME = "@shop100sho"

# ✅ رابط Webhook الخاص بـ Render
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://telegram-shop-bot-lo4t.onrender.com/")

# ✅ إعدادات إضافية
ADMINS = [
    {"id": 6935846121, "name": "حسين", "username": "@Houssin363", "shift": "أساسي"},
    # أضف الأدمن الثاني والثالث هنا لاحقاً حسب التوقيت
]

# ✅ إعدادات عامة
LANG = "ar"
ENCODING = "utf-8"

# ⚖️ سعر صرف PAYEER
PAYEER_RATE = 9000  # كل 1 بايير = 9000 ل.س

# ✅ إعدادات Supabase
SUPABASE_URL = "https://azortroeejjomqweintc.supabase.co"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF6b3J0cm9lZWpqb21xd2VpbnRjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTIxOTIzNjUsImV4cCI6MjA2Nzc2ODM2NX0.x3Pwq8OyRmlr7JQuEU2xRxYJtSoz67eIVzDx8Nh4muk"
SUPABASE_TABLE_NAME = "houssin363"
