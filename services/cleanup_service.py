# services/cleanup_service.py
# حذف محافظ خاملة حسب عدد الأيام، بايثون فقط عبر supabase-py / PostgREST
# لا يعتمد على SQL خام. يفترض أن لديك SUPABASE_KEY = service_role في .env

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Iterable
from database.db import get_table

def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def _cutoff_iso(days: int) -> str:
    return _utc_iso(datetime.now(timezone.utc) - timedelta(days=days))

def _chunked(seq: Iterable[int], size: int):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i+size]

def preview_inactive_users(
    days: int = 33,
    require_zero_balances: bool = False,
    limit: int = 100_000
) -> List[Dict[str, Any]]:
    """
    يرجّع صفوف المستخدمين الخاملين المؤهّلين للحذف (لا يحذف).
    الشروط:
      - updated_at <= now - days
      - لا نشاط حديث في transactions.timestamp ولا purchases.created_at بعد cutoff
      - لا يوجد pending_requests للمستخدم
    ملاحظة: لا نتحقق من balance/held إلا إذا require_zero_balances=True.
    """
    cutoff = _cutoff_iso(days)

    # 1) المرشّحون بالزمن فقط (+ اختياريًا balance/held)
    q = (
        get_table("houssin363")
        .select("user_id, balance, held, updated_at")
        .lte("updated_at", cutoff)
        .limit(limit)
    )
    if require_zero_balances:
        q = q.eq("balance", 0).eq("held", 0)

    res = q.execute()
    rows = res.data or []
    candidate_ids = [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
    if not candidate_ids:
        return []

    # 2) استبعاد من لديه طلبات معلّقة
    pr = (
        get_table("pending_requests")
        .select("user_id")
        .in_("user_id", candidate_ids)
        .execute()
    )
    pending_ids = {int(r["user_id"]) for r in (pr.data or [])}

    # 3) استبعاد من لديه معاملات بعد cutoff
    tr = (
        get_table("transactions")
        .select("user_id")
        .in_("user_id", candidate_ids)
        .gte("timestamp", cutoff)
        .execute()
    )
    trans_ids = {int(r["user_id"]) for r in (tr.data or [])}

    # 4) استبعاد من لديه مشتريات بعد cutoff
    pu = (
        get_table("purchases")
        .select("user_id")
        .in_("user_id", candidate_ids)
        .gte("created_at", cutoff)
        .execute()
    )
    purch_ids = {int(r["user_id"]) for r in (pu.data or [])}

    exclude = pending_ids | trans_ids | purch_ids
    eligible = [r for r in rows if int(r["user_id"]) not in exclude]
    return eligible

def delete_inactive_users(
    days: int = 33,
    require_zero_balances: bool = False,
    batch_size: int = 500
) -> List[int]:
    """
    يحذف فعليًا من h oussin363 كل من رجّعتهم preview_inactive_users.
    يرجّع قائمة user_id التي حُذفت.
    """
    eligible_rows = preview_inactive_users(days=days, require_zero_balances=require_zero_balances, limit=100_000)
    ids = [int(r["user_id"]) for r in eligible_rows]
    if not ids:
        return []

    deleted_ids: List[int] = []
    for chunk in _chunked(ids, batch_size):
        get_table("houssin363").delete().in_("user_id", chunk).execute()
        deleted_ids.extend(chunk)
    return deleted_ids

# تشغيل يدوي سريع:
if __name__ == "__main__":
    # بروفة: مين سيتحذف؟
    preview = preview_inactive_users(days=33, require_zero_balances=False)
    print(f"[DRY] candidates={len(preview)}")
    # للحذف الفعلي أزل التعليق:
    # deleted = delete_inactive_users(days=33, require_zero_balances=False)
    # print(f"[DELETE] deleted={len(deleted)} users: {deleted[:50]}{'...' if len(deleted)>50 else ''}")
