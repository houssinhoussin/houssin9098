# services/maintenance_worker.py
import threading
from datetime import datetime, timedelta
from database.db import get_table

OUTBOX = "notifications_outbox"
WALLETS = "houssin363"

# الجداول القصيرة العمر (استثناء الإعلانات كما طلبت)
SHORT_TABLES = [
    "bill_and_units_purchases",
    "cash_transfer_purchases",
    "companies_transfer_purchases",
    "game_purchases",
    "internet_providers_purchases",
    "university_fees_purchases",
    "wholesale_purchases",
    # "ads_purchases" ← مستثناة
]

def cleanup_short_lived_tables():
    """حذف سجلات أقدم من 14 ساعة للجداول المحددة + استثناء pending_requests[type='ads']."""
    threshold = (datetime.utcnow() - timedelta(hours=14)).isoformat()
    for t in SHORT_TABLES:
        try:
            get_table(t).delete().lte("created_at", threshold).execute()
        except Exception:
            pass
    try:
        get_table("pending_requests") \
            .delete() \
            .lte("created_at", threshold) \
            .neq("type", "ads") \
            .execute()
    except Exception:
        pass

def _iso_to_dt(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None

def _last_activity_for_user(user_id: int):
    """أعلى طابع زمني لأي نشاط معروف للمستخدم."""
    candidates = []
    def bump(tbl, col="created_at"):
        try:
            r = get_table(tbl).select(col).eq("user_id", user_id).order(col, desc=True).limit(1).execute().data or []
            if r and r[0].get(col):
                dt = _iso_to_dt(r[0][col])
                if dt: candidates.append(dt)
        except Exception:
            pass

    # مصادر النشاط
    for t in ["purchases","holds","ads_purchases","bill_and_units_purchases","cash_transfer_purchases",
              "companies_transfer_purchases","internet_providers_purchases","university_fees_purchases",
              "wholesale_purchases","pending_requests"]:
        bump(t)
    bump("transactions", col="timestamp")

    # created_at/updated_at من جدول المحافظ نفسه
    try:
        u = get_table(WALLETS).select("created_at,updated_at").eq("user_id", user_id).single().execute().data or {}
        for k in ("created_at","updated_at"):
            dt = _iso_to_dt(u.get(k))
            if dt: candidates.append(dt)
    except Exception:
        pass

    return max(candidates) if candidates else None

def _enqueue(user_id: int, template: str, payload: dict | None = None):
    try:
        get_table(OUTBOX).insert({
            "user_id": user_id,
            "template": template,
            "payload": payload or {},
            "scheduled_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception:
        pass

def wallet_notifications_and_cleanup():
    """تنبيهات 6/3/0 أيام ثم حذف المحفظة بعد 33 يوم خمول — بلا أي خصم/إضافة أموال."""
    try:
        users = get_table(WALLETS).select("user_id,created_at,updated_at").execute().data or []
    except Exception:
        users = []

    now = datetime.utcnow()
    for u in users:
        uid = u["user_id"]
        last = _last_activity_for_user(uid)
        if not last:
            continue
        days = int((now - last).total_seconds() // 86400)

        if days == 27: _enqueue(uid, "wallet_delete_6d", {"days_left": 6})
        elif days == 30: _enqueue(uid, "wallet_delete_3d", {"days_left": 3})
        elif days == 32: _enqueue(uid, "wallet_delete_0d", {"days_left": 1})
        elif days >= 33:
            try:
                get_table(WALLETS).delete().eq("user_id", uid).execute()
            except Exception:
                pass
            _enqueue(uid, "wallet_deleted", {})

def start_housekeeping(bot=None):
    """ابدأ عاملين: تنظيف كل ساعة + فحص المحافظ يوميًا 06:00 بتوقيت دمشق."""
    # 1) تنظيف كل ساعة
    def hourly():
        try:
            cleanup_short_lived_tables()
        finally:
            threading.Timer(3600, hourly).start()
    hourly()

    # 2) فحص يومي عند 06:00 دمشق
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Damascus")
    except Exception:
        tz = None

    def _daily():
        try:
            wallet_notifications_and_cleanup()
        finally:
            threading.Timer(24*3600, _daily).start()

    # احسب تأخير أول تشغيل حتى 06:00 دمشق
    if tz:
        now = datetime.now(tz)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        threading.Timer(delay, _daily).start()
    else:
        # لو مكتبة المنطقة غير متاحة، شغّل يوميًا اعتبارًا من الآن
        threading.Timer(1, _daily).start()
