# services/scheduled_tasks.py
import logging
from datetime import datetime, timedelta
import threading
import time
from typing import Optional

from database.db import get_table
from config import (
    TABLE_USERS,
    TABLE_TRANSACTIONS,
    TABLE_PURCHASES,
    BOT_USERNAME,
)

# كم يوم يُعتبر بعدها الحساب غير نشط
DELETE_USER_AFTER_DAYS   = 35
# نحذّر قبل الحذف بـ X أيام
WARN_USER_BEFORE_DAYS    = 5
# حذف سجلات قديمة (معاملات/مشتريات) أقدم من X أيام
DELETE_RECORDS_AFTER_DAYS = 7

BOT_LINK = f"https://t.me/{BOT_USERNAME}"  # رابط البوت الحقيقي من الإعدادات

# -----------------------------------------------------
# أدوات مساعدة
# -----------------------------------------------------
def _parse_iso(ts) -> Optional[datetime]:
    """يحاول تحويل نص ISO8601 إلى datetime (UTC مفترض)."""
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        # قص الثواني الزائدة لو فيه ميكروثانية
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None

def _now() -> datetime:
    return datetime.utcnow()

# -----------------------------------------------------
# إرسال تحذير للمستخدم
# -----------------------------------------------------
def send_warning_message(bot, user_id: int, delete_date: datetime):
    """إرسال تحذير للمستخدم قبل حذف حسابه. يتجاهل أي خطأ (حظر/حذف)."""
    try:
        warning_text = (
            "🚨 <b>تنبيه مهم</b>\n"
            f"سيتم حذف حسابك وجميع بياناتك بتاريخ <b>{delete_date.strftime('%Y-%m-%d')}</b> "
            f"لعدم وجود نشاط في محفظتك لمدة {DELETE_USER_AFTER_DAYS} يوم.\n\n"
            "للاحتفاظ بحسابك: قم بشحن المحفظة أو نفّذ عملية شراء قبل هذا التاريخ.\n"
            "بعد الحذف لا يمكن استعادة الرصيد أو البيانات.\n\n"
            f"رابط البوت: {BOT_LINK}"
        )
        bot.send_message(user_id, warning_text, parse_mode="HTML")
        logging.info(f"[MAINT] أُرسل تحذير حذف للمستخدم {user_id}")
    except Exception as e:
        logging.warning(f"[MAINT] تعذّر إرسال تحذير للمستخدم {user_id}: {e}")

# -----------------------------------------------------
# حذف مستخدمين غير نشطين
# -----------------------------------------------------
def delete_inactive_users(bot):
    """
    يحذف المستخدمين غير النشطين منذ X يوم.
    ويرسل تحذير قبل 5 أيام من الحذف الفعلي.
    """
    now = _now()

    users_resp = get_table(TABLE_USERS).select("*").execute()
    rows = users_resp.data or []
    if not rows:
        return

    for user in rows:
        user_id = user.get("user_id")
        if not user_id:
            continue

        # آخر نشاط: من المعاملات/المشتريات أو تاريخ الإنشاء إن وُجد
        created_at = _parse_iso(user.get("created_at"))
        last_activity = created_at or now

        last_txn = (
            get_table(TABLE_TRANSACTIONS)
            .select("timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        if last_txn.data:
            ts = _parse_iso(last_txn.data[0].get("timestamp"))
            if ts and (not last_activity or ts > last_activity):
                last_activity = ts

        last_purchase = (
            get_table(TABLE_PURCHASES)
            .select("created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if last_purchase.data:
            ts = _parse_iso(last_purchase.data[0].get("created_at"))
            if ts and (not last_activity or ts > last_activity):
                last_activity = ts

        if not last_activity:
            last_activity = now

        days_inactive = (now - last_activity).days

        # أ) تحذير قبل الحذف
        if DELETE_USER_AFTER_DAYS - WARN_USER_BEFORE_DAYS <= days_inactive < DELETE_USER_AFTER_DAYS:
            delete_date = last_activity + timedelta(days=DELETE_USER_AFTER_DAYS)
            send_warning_message(bot, user_id, delete_date)

        # ب) حذف بعد انقضاء المدة
        elif days_inactive >= DELETE_USER_AFTER_DAYS:
            try:
                get_table(TABLE_USERS).delete().eq("user_id", user_id).execute()
                get_table(TABLE_TRANSACTIONS).delete().eq("user_id", user_id).execute()
                get_table(TABLE_PURCHASES).delete().eq("user_id", user_id).execute()
                logging.info(f"[MAINT] حُذف المستخدم {user_id} نهائيًا بسبب عدم النشاط.")
            except Exception as e:
                logging.error(f"[MAINT] فشل حذف المستخدم {user_id}: {e}", exc_info=True)

# -----------------------------------------------------
# حذف سجلات قديمة
# -----------------------------------------------------
def delete_old_transactions_and_purchases():
    """حذف السجلات الأقدم من X أيام من جدولَي المعاملات والمشتريات."""
    cutoff = _now() - timedelta(days=DELETE_RECORDS_AFTER_DAYS)
    cutoff_iso = cutoff.isoformat()

    try:
        get_table(TABLE_TRANSACTIONS).delete().lt("timestamp", cutoff_iso).execute()
        get_table(TABLE_PURCHASES).delete().lt("created_at", cutoff_iso).execute()
        logging.info("[MAINT] تم حذف السجلات القديمة من transactions و purchases.")
    except Exception as e:
        logging.error(f"[MAINT] فشل حذف السجلات القديمة: {e}", exc_info=True)

# -----------------------------------------------------
# حلقة الجدولة اليومية
# -----------------------------------------------------
def _daily_loop(bot):
    while True:
        try:
            logging.info("[MAINT] تشغيل المهام الدورية: تنظيف المستخدمين والسجلات…")
            delete_inactive_users(bot)
            delete_old_transactions_and_purchases()
        except Exception as e:
            logging.error(f"[MAINT] خطأ في المهام الدورية: {e}", exc_info=True)
        # انتظر 24 ساعة
        time.sleep(86400)

def start_daily_maintenance(bot):
    """استدعِها من main.py لتشغيل المهام الدورية في الخلفية."""
    threading.Thread(target=_daily_loop, args=(bot,), daemon=True).start()
    logging.info("[MAINT] تم تشغيل جدولة المهام اليومية في الخلفية.")
