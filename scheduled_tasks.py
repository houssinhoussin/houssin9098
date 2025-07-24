# === scheduled_tasks.py ===
import logging
from datetime import datetime, timedelta
import threading
import time

from database.db import client

import telebot
from config import API_TOKEN

# إعداد كائن البوت لإرسال الرسائل
bot = telebot.TeleBot(API_TOKEN)

# اسماء الجداول في supabase
USERS_TABLE = "houssin363"
TRANSACTIONS_TABLE = "transactions"
PURCHASES_TABLE = "purchases"

# حذف السجلات الأقدم من X يوم
DELETE_USER_AFTER_DAYS = 35
WARN_USER_BEFORE_DAYS = 5
DELETE_RECORDS_AFTER_DAYS = 7

BOT_LINK = "https://t.me/اسم_البوت_هنا"  # ضع رابط البوت الخاص بك هنا

def send_warning_message(user_id, delete_date):
    """
    يحاول إرسال تحذير للمستخدم قبل حذف حسابه.
    يتجاهل الخطأ إذا كان المستخدم قد حظر البوت أو حذفه.
    """
    try:
        warning_text = (
            f"🚨 تنبيه!\n"
            f"سيتم حذف حسابك وجميع بياناتك من النظام بتاريخ {delete_date.strftime('%Y-%m-%d')} "
            f"لعدم وجود نشاط في محفظتك لمدة {DELETE_USER_AFTER_DAYS} يوم.\n"
            "إذا كنت تريد الحفاظ على حسابك، يرجى شحن محفظتك أو تنفيذ عملية شراء قبل هذا التاريخ.\n"
            "بعد الحذف لا يمكنك المطالبة بأي رصيد أو مراجعة.\n\n"
            f"رابط البوت: {BOT_LINK}"
        )
        bot.send_message(user_id, warning_text)
        logging.info(f"تم إرسال تحذير للحذف للمستخدم {user_id}")
    except Exception as e:
        # يتجاهل أي خطأ (حظر البوت أو حذف البوت)
        logging.warning(f"فشل إرسال تحذير للمستخدم {user_id}: {e}")

def delete_inactive_users():
    """
    يحذف المستخدمين غير النشطين منذ X يوم.
    ويرسل تحذير قبل 5 أيام من الحذف الفعلي.
    """
    now = datetime.utcnow()
    # 1) استخرج كل المستخدمين
    users_resp = client.table(USERS_TABLE).select("*").execute()
    if not users_resp.data:
        return

    for user in users_resp.data:
        user_id = user.get("user_id")
        created_at = user.get("created_at")
        last_activity = created_at
        # تحقق من آخر معاملة أو شراء له
        last_txn = client.table(TRANSACTIONS_TABLE)\
            .select("timestamp")\
            .eq("user_id", user_id)\
            .order("timestamp", desc=True)\
            .limit(1)\
            .execute()
        last_purchase = client.table(PURCHASES_TABLE)\
            .select("created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        if last_txn.data:
            last_activity = max(last_activity, last_txn.data[0]["timestamp"])
        if last_purchase.data:
            last_activity = max(last_activity, last_purchase.data[0]["created_at"])
        # تحويل آخر نشاط لـ datetime
        last_dt = (
            datetime.strptime(last_activity[:19], "%Y-%m-%dT%H:%M:%S")
            if isinstance(last_activity, str)
            else last_activity
        )
        days_inactive = (now - last_dt).days
        # أ) أرسل تحذير قبل 5 أيام
        if DELETE_USER_AFTER_DAYS - WARN_USER_BEFORE_DAYS <= days_inactive < DELETE_USER_AFTER_DAYS:
            delete_date = last_dt + timedelta(days=DELETE_USER_AFTER_DAYS)
            send_warning_message(user_id, delete_date)
        # ب) حذف العميل بعد المدة
        elif days_inactive >= DELETE_USER_AFTER_DAYS:
            # حذف من جميع الجداول المتعلقة
            client.table(USERS_TABLE).delete().eq("user_id", user_id).execute()
            client.table(TRANSACTIONS_TABLE).delete().eq("user_id", user_id).execute()
            client.table(PURCHASES_TABLE).delete().eq("user_id", user_id).execute()
            logging.info(f"تم حذف المستخدم {user_id} نهائيًا بسبب عدم النشاط.")

def delete_old_transactions_and_purchases():
    """
    حذف جميع السجلات الأقدم من X أيام من جدول المعاملات والشراء.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=DELETE_RECORDS_AFTER_DAYS)
    # حذف المعاملات القديمة
    client.table(TRANSACTIONS_TABLE)\
        .delete()\
        .lt("timestamp", cutoff.isoformat())\
        .execute()
    # حذف المشتريات القديمة
    client.table(PURCHASES_TABLE)\
        .delete()\
        .lt("created_at", cutoff.isoformat())\
        .execute()
    logging.info("تم حذف السجلات القديمة من جدول المعاملات والمشتريات.")

def run_scheduled_tasks():
    """
    دالة رئيسية: تكرر المهام كل يوم تلقائيًا في الخلفية.
    """
    while True:
        try:
            logging.info("تشغيل المهام الدورية: حذف العملاء غير النشطين وحذف السجلات القديمة.")
            delete_inactive_users()
            delete_old_transactions_and_purchases()
        except Exception as e:
            logging.error(f"خطأ في المهام الدورية: {e}")
        # انتظر 24 ساعة (86400 ثانية)
        time.sleep(86400)

# اطلاق الثريد عند استيراد الملف تلقائيًا
threading.Thread(target=run_scheduled_tasks, daemon=True).start()
# === نهاية الملف ===
