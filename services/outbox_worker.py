# services/outbox_worker.py
import threading, logging
from datetime import datetime
from database.db import get_table
from services.notification_service import notify_user

TBL = "notifications_outbox"

TEMPLATES = {
    "wallet_delete_6d": lambda p: (
        "تنبيه هام: سيتم حذف محفظتك بعد 6 أيام بسبب عدم استخدامها لمدة طويلة.\n"
        "وفق سياسة خدماتنا، تُحذف المحافظ الخاملة 33 يومًا.\n"
        "لتجديد المهلة يكفي تنفيذ عملية واحدة (إيداع/سحب/شراء/تحويل)."
    ),
    "wallet_delete_3d": lambda p: (
        "تنبيه: بقي 3 أيام على حذف محفظتك لعدم الاستخدام.\n"
        "نفّذ أي عملية الآن لتجديد المهلة تلقائيًا."
    ),
    "wallet_delete_0d": lambda p: (
        "اليوم الأخير قبل حذف محفظتك لعدم الاستخدام.\n"
        "ملاحظة: لسنا مسؤولين عن أي مبالغ بعد انتهاء المهلة."
    ),
    "wallet_deleted":  lambda p: (
        "تم حذف محفظتك بسبب عدم استخدامها لمدة 33 يومًا.\n"
        "لا يمكننا مراجعة مبالغ بعد هذه المدة حسب سياسة الخدمة."
    ),
}

def _render(template, payload):
    f = TEMPLATES.get(template)
    return f(payload) if f else None

def process_outbox(bot, batch=50):
    rows = (
        get_table(TBL)
        .select("id,user_id,template,payload,scheduled_at")
        .is_("sent_at", None)
        .lte("scheduled_at", datetime.utcnow().isoformat())
        .limit(batch)
        .execute()
        .data
        or []
    )
    for r in rows:
        text = _render(r["template"], r.get("payload") or {})
        if not text:  # قالب غير معروف
            logging.warning(f"Unknown template {r['template']}")
            get_table(TBL).update({"sent_at": datetime.utcnow().isoformat()}).eq("id", r["id"]).execute()
            continue
        try:
            notify_user(bot, r["user_id"], text)
        finally:
            get_table(TBL).update({"sent_at": datetime.utcnow().isoformat()}).eq("id", r["id"]).execute()

def start_outbox_worker(bot, interval_sec=60):
    def _tick():
        try:
            process_outbox(bot)
        except Exception:
            logging.exception("outbox worker error")
        finally:
            threading.Timer(interval_sec, _tick).start()
    _tick()
