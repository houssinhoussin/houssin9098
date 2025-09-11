# services/cleanup_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import threading
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from database.db import get_table

# الجداول التي تُحذف تلقائيًا بعد 14 ساعة (مع استثناء USERS_TABLE كليًا)
EPHEMERAL_TABLES: List[str] = [
    # كل أنواع المشتريات
    "purchases",
    "game_purchases",
    "ads_purchases",
    "bill_and_units_purchases",
    "cash_transfer_purchases",
    "companies_transfer_purchases",
    "internet_providers_purchases",
    "university_fees_purchases",
    "wholesale_purchases",

    # سجلات التحويل/المعاملات
    "transactions",

    # سجلات المنتجات
    "products",

    # جديد
    "holds",
    "user_state",
]

# جداول تُعدّ نشاطًا للمستخدم (لتقييم خمول المحفظة)
ACTIVITY_TABLES = {
    "transactions": "timestamp",
    "purchases": "created_at",
}

# ====== منطق إعادة المحاولة (backoff خفيف) للتعامل مع أخطاء الشبكة/HTTP2 ======
_RETRIES = 3
_BACKOFF = 0.8  # ثوانٍ

def _with_retry(op, *args, **kwargs):
    for i in range(_RETRIES):
        try:
            return op(*args, **kwargs)
        except Exception as e:
            # أخطاء المخطط (كالعمود غير الموجود) ليست مؤقتة: لا نعيد المحاولة عليها
            msg = str(e)
            if "42703" in msg or "does not exist" in msg.lower():
                raise
            if i == _RETRIES - 1:
                raise
            logging.warning("cleanup_service retry %s/%s: %s", i + 1, _RETRIES, e)
            time.sleep(_BACKOFF * (i + 1))

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def _cutoff(hours: Optional[int] = None, days: Optional[int] = None) -> datetime:
    base = _utc_now()
    if hours is not None:
        return base - timedelta(hours=hours)
    if days is not None:
        return base - timedelta(days=days)
    return base

# ====== فحص وجود العمود قبل التنفيذ لتجنب أخطاء 42703 ======
def _column_exists(table_name: str, col: str) -> bool:
    try:
        # حدّ علوي صفر: استعلام خفيف فقط لاختبار وجود العمود
        get_table(table_name).select(col).limit(0).execute()
        return True
    except Exception as e:
        msg = str(e)
        if "42703" in msg or "does not exist" in msg.lower():
            # عمود غير موجود: نُعلم المنادي كي يتخطّاه بدون أي إعادة محاولات
            return False
        # خطأ آخر مؤقت (شبكة/تحميل): لا نعرقل المنادي؛ نعتبره موجودًا ونترك
        # عملية DELETE تتولّى إعادة المحاولة عبر _with_retry.
        print(f"[cleanup] column probe {table_name}.{col} error (ignored): {e}")
        return True

def _safe_delete_by(table_name: str, col: str, cutoff_iso: str) -> tuple[bool, int]:
    """
    يرجع (executed_ok, count)
    - executed_ok=True يعني تم تنفيذ DELETE على هذا العمود بدون خطأ،
      حتى لو لم تُرجِع Supabase صفوفًا (count=0).
    - executed_ok=False يعني أن العمود غير موجود أو فشل غير قابل لإعادة المحاولة،
      وعلى المنادي تجربة عمود احتياطي.
    """
    # أولًا: لا نجرب الحذف إن كان العمود غير موجود
    if not _column_exists(table_name, col):
        return False, 0
    try:
        resp = _with_retry(get_table(table_name).delete().lte(col, cutoff_iso).execute)
        data = getattr(resp, "data", None)
        count = len(data) if isinstance(data, list) else 0
        return True, count
    except Exception as e:
        msg = str(e)
        # أخطاء المخطط: نُشير للمنادي ليتحوّل إلى عمودٍ احتياطي دون تحذيرات متكررة
        if "42703" in msg or "does not exist" in msg.lower():
            print(f"[cleanup] skip non-existent column {table_name}.{col}")
            return False, 0
        print(f"[cleanup] delete error on {table_name}.{col}: {e}")
        return False, 0

def _delete_with_fallbacks(table_name: str, cutoff_iso: str, now_iso: str) -> int:
    """
    منطق الحذف:
      1) إن وُجد عمود expire_at نحذف ما انتهى (expire_at <= الآن).
      2) وإلا نحذف الأقدم من مدة القطع عبر created_at ثم timestamp ثم updated_at.
      * نتوقّف عند أول تنفيذ ناجح (حتى لو صفر صفوف).
      * عند عدم وجود العمود، نتخطّاه فورًا بدون إعادة محاولات مزعجة.
    """
    order = [
        ("expire_at", now_iso),
        ("created_at", cutoff_iso),
        ("timestamp", cutoff_iso),
        ("updated_at", cutoff_iso),
    ]
    for col, when in order:
        executed, count = _safe_delete_by(table_name, col, when)
        if executed:
            return max(count, 0)
    return 0

def purge_ephemeral_after(hours: int = 14) -> Dict[str, int]:
    """حذف سجلات الجداول المؤقتة بعد 14 ساعة."""
    res: Dict[str, int] = {}
    now_iso = _iso(_utc_now())
    cutoff_iso = _iso(_cutoff(hours=hours))
    for tbl in EPHEMERAL_TABLES:
        count = _delete_with_fallbacks(tbl, cutoff_iso, now_iso)
        res[tbl] = max(count, 0)
    return res

def _has_activity_since(user_id: int, since_iso: str) -> bool:
    for tbl, col in ACTIVITY_TABLES.items():
        # إذا كان عمود النشاط غير موجود في الجدول، نتخطّاه
        if not _column_exists(tbl, col):
            continue
        try:
            r = _with_retry(
                get_table(tbl).select("id").eq("user_id", user_id).gte(col, since_iso).limit(1).execute
            )
            if getattr(r, "data", None):
                return True
        except Exception as e:
            print(f"[cleanup] activity probe error {tbl}.{col} for {user_id}: {e}")
            continue
    return False

def preview_inactive_users(days: int = 33, limit: int = 100_000) -> List[Dict[str, Any]]:
    """إظهار المحافظ الخاملة المرشحة للحذف بعد X يوم (لا يحذف فعليًا)."""
    cutoff_iso = _iso(_cutoff(days=days))
    rows: List[Dict[str, Any]] = []
    try:
        resp = _with_retry(
            get_table(USERS_TABLE).select("user_id, updated_at, created_at").lte("updated_at", cutoff_iso).limit(limit).execute
        )
        rows = getattr(resp, "data", None) or []
    except Exception:
        try:
            resp = _with_retry(
                get_table(USERS_TABLE).select("user_id, created_at").lte("created_at", cutoff_iso).limit(limit).execute
            )
            rows = getattr(resp, "data", None) or []
        except Exception as e:
            print(f"[cleanup] select USERS_TABLE failed: {e}")
            return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        uid = r.get("user_id")
        if uid is None:
            continue
        if not _has_activity_since(int(uid), cutoff_iso):
            out.append(r)
    return out

def delete_inactive_users(days: int = 33, batch_size: int = 500) -> List[int]:
    """حذف فعلي لمحافظ USERS_TABLE الخاملة 33 يومًا بعد التحذيرات."""
    candidates = preview_inactive_users(days=days)
    ids = [int(r["user_id"]) for r in candidates if r.get("user_id") is not None]
    if not ids:
        return []
    deleted: List[int] = []
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i+batch_size]
        try:
            _with_retry(get_table(USERS_TABLE).delete().in_("user_id", chunk).execute)
            deleted.extend(chunk)
        except Exception as e:
            print(f"[cleanup] delete USERS_TABLE chunk failed: {e}")
            continue
    return deleted

def _housekeeping_tick(bot=None):
    try:
        purged = purge_ephemeral_after(hours=14)
        print(f"[cleanup] purged (14h): {purged}")
    except Exception as e:
        print(f"[cleanup] purge_ephemeral_after error: {e}")

    try:
        deleted = delete_inactive_users(days=33)
        if deleted:
            print(f"[cleanup] deleted USERS_TABLE users: {len(deleted)}")
    except Exception as e:
        print(f"[cleanup] delete_inactive_users error: {e}")

def schedule_housekeeping(bot=None, every_seconds: int = 3600):
    """يشغّل التنظيف كل ساعة بخيط منفصل."""
    def _loop():
        _housekeeping_tick(bot)
        threading.Timer(every_seconds, _loop).start()
    threading.Timer(60, _loop).start()
