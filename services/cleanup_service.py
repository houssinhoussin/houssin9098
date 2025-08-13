# services/cleanup_service.py
# حذف المحافظ/المستخدمين الخاملين بعد مدة محددة.
# ترجع عدد السجلات المحذوفة. لا تعتمد على pg_cron.
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List
from database.db import get_table

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

def _has_recent_activity(user_id: int, since_iso: str) -> bool:
    """
    فحص سريع إن كان للمستخدم نشاط حديث (شراء/تحويل/ترانزكشن) يمنع الحذف.
    نحافظ على عدد استدعاءات قليل بحد 1 لكل جدول.
    """
    try:
        # 1) معاملات عامة
        tr = (
            get_table("transactions")
            .select("id")
            .eq("user_id", user_id)
            .gte("timestamp", since_iso)
            .limit(1)
            .execute()
        )
        if tr.data:
            return True

        # 2) مشتريات
        pr = (
            get_table("purchases")
            .select("id")
            .eq("user_id", user_id)
            .gte("created_at", since_iso)
            .limit(1)
            .execute()
        )
        if pr.data:
            return True

        # 3) طلبات معلّقة (لو فيه طلب قيد الانتظار، لا نحذف)
        pq = (
            get_table("pending_requests")
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if pq.data:
            return True

    except Exception:
        # في حال الفشل نكون حذرين: نعتبره لديه نشاط (لا نحذف)
        return True

    return False

def delete_inactive_users(days: int = 33, dry_run: bool = False) -> int:
    """
    يحذف من جدول المحافظ (houssin363) كل مستخدم:
     - balance = 0 و held = 0
     - updated_at <= now - days
     - لا نشاط حديث في الجداول المرتبطة
    ترجع عدد السجلات المحذوفة. لو dry_run=True تُرجع العدد المتوقع بدون حذف فعلي.
    """
    since_iso = _cutoff(days)
    deleted = 0
    try:
        # نجلب المرشحين للحذف
        res = (
            get_table("houssin363")
            .select("user_id, balance, held, updated_at")
            .eq("balance", 0)
            .eq("held", 0)
            .lte("updated_at", since_iso)
            .limit(10000)
            .execute()
        )
        if not getattr(res, "data", None):
            return 0

        for row in res.data:
            uid = int(row["user_id"])
            # تأكّد مرة أخيرة من عدم وجود نشاط حديث
            if _has_recent_activity(uid, since_iso):
                continue
            if dry_run:
                deleted += 1
            else:
                get_table("houssin363").delete().eq("user_id", uid).execute()
                deleted += 1

        return deleted
    except Exception as e:
        # يمكنك طباعة الخطأ إلى اللوجز إن أحببت
        print(f"[cleanup_service] error: {e}")
        return deleted
