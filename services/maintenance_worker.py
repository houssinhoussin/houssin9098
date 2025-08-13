# -*- coding: utf-8 -*-
# services/maintenance_worker.py
from __future__ import annotations
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from database.db import get_table
from services.cleanup_service import purge_ephemeral_after, preview_inactive_users, delete_inactive_users

OUTBOX_TABLE = "notifications_outbox"

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _now_iso() -> str:
    return _now().isoformat()

def _insert_outbox_if_absent(user_id: int, message: str, kind: str, when_iso: str):
    """
    يمنع التكرار لنفس (user_id, kind) إذا كانت رسالة بنفس النوع غير مرسلة بعد.
    """
    try:
        exists = (
            get_table(OUTBOX_TABLE)
            .select("id")
            .eq("user_id", user_id)
            .eq("kind", kind)
            .is_("sent_at", None)
            .limit(1)
            .execute()
        )
        if exists.data:
            return
    except Exception:
        # لو الجدول لا يوجد، نتجاهل التنبيه بصمت
        return
    try:
        get_table(OUTBOX_TABLE).insert({
            "user_id": user_id,
            "message": message,
            "kind": kind,
            "scheduled_at": when_iso,
            "created_at": _now_iso(),
            "parse_mode": "HTML",
        }).execute()
    except Exception as e:
        print(f"[maintenance] insert outbox failed: {e}")

def _warn_text(days_left: int) -> str:
    if days_left == 6:
        return (
            "⏰ <b>تنبيه</b>\n"
            "سيتم حذف محفظتك بعد <b>6 أيام</b> بسبب عدم النشاط لمدة 33 يومًا.\n"
            "✅ أي نشاط (عملية واحدة فقط) يعيد المهلة من جديد.\n"
            "نوصيك بسحب/صرف رصيدك أو تنفيذ عملية بسيطة لتفادي الحذف."
        )
    if days_left == 3:
        return (
            "⏰ <b>تنبيه مهم</b>\n"
            "يتبقى <b>3 أيام</b> قبل حذف محفظتك لعدم النشاط (33 يومًا).\n"
            "✅ نفّذ أي عملية الآن لتجديد المهلة، أو اسحب رصيدك إن وُجد."
        )
    # اليوم الأخير
    return (
        "⚠️ <b>اليوم الأخير</b>\n"
        "سيتم حذف محفظتك اليوم بسبب عدم النشاط لمدة 33 يومًا.\n"
        "تنويه: لسنا مسؤولين عن أي مبلغ بعد انتهاء مدة التحذير.\n"
        "من سياسة خدماتنا: حذف المحفظة عند وجود جمود لمدة 33 يومًا.\n"
        "سارع بتنفيذ أي عملية لتجديد المهلة (حتى عملية واحدة تكفي)."
    )

def _process_wallet_warnings():
    """
    ينشئ تنبيهات 6 و3 واليوم الأخير للمحافظ الخاملة.
    """
    # مرشحو اليوم الأخير (33 يوم خمول)
    final_candidates = preview_inactive_users(days=33)
    for r in final_candidates:
        uid = int(r["user_id"])
        _insert_outbox_if_absent(uid, _warn_text(0), "wallet_delete_0d", _now_iso())

    # مرشحو 3 أيام (30 يوم خمول)
    in3_candidates = preview_inactive_users(days=30)
    for r in in3_candidates:
        uid = int(r["user_id"])
        _insert_outbox_if_absent(uid, _warn_text(3), "wallet_delete_3d", _now_iso())

    # مرشحو 6 أيام (27 يوم خمول)
    in6_candidates = preview_inactive_users(days=27)
    for r in in6_candidates:
        uid = int(r["user_id"])
        _insert_outbox_if_absent(uid, _warn_text(6), "wallet_delete_6d", _now_iso())

def _housekeeping_once(bot=None):
    try:
        # 1) تنظيف سجلات مؤقتة بعد 14 ساعة
        purged = purge_ephemeral_after(hours=14)
        print(f"[maintenance] purged_14h: {purged}")
    except Exception as e:
        print(f"[maintenance] purge_ephemeral_after error: {e}")

    try:
        # 2) إرسال تحذيرات 6/3/0 أيام
        _process_wallet_warnings()
    except Exception as e:
        print(f"[maintenance] warn generation error: {e}")

    try:
        # 3) حذف المحافظ الخاملة 33 يومًا (بغض النظر عن الرصيد/المحجوز)
        deleted = delete_inactive_users(days=33)
        if deleted:
            # أرسل إشعار "تم الحذف" (اختياري)
            msg = (
                "🗑️ <b>تم حذف محفظتك</b>\n"
                "بسبب عدم النشاط لمدة 33 يومًا بعد إرسال التحذيرات.\n"
                "لا يمكن مراجعتنا بهذا الخصوص وفق سياسة الخدمة."
            )
            for uid in deleted:
                _insert_outbox_if_absent(int(uid), msg, "wallet_deleted", _now_iso())
            print(f"[maintenance] deleted wallets: {len(deleted)}")
    except Exception as e:
        print(f"[maintenance] delete_inactive_users error: {e}")

def start_housekeeping(bot=None, every_seconds: int = 3600):
    """
    عامل صيانة دوري داخل التطبيق (بديل pg_cron):
     - تنظيف 14 ساعة
     - تحذيرات حذف المحفظة (6/3/0)
     - حذف المحافظ 33 يوم خمول
    """
    def loop():
        _housekeeping_once(bot)
        threading.Timer(every_seconds, loop).start()
    # التشغيل الأول بعد دقيقة من الإقلاع
    threading.Timer(60, loop).start()
