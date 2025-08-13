# services/cleanup_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Iterable, Optional
from database.db import get_table

EPHEMERAL_TABLES: List[str] = [
    "purchases",
    "game_purchases",
    "ads_purchases",
    "bill_and_units_purchases",
    "cash_transfer_purchases",
    "companies_transfer_purchases",
    "internet_providers_purchases",
    "university_fees_purchases",
    "wholesale_purchases",
]

ACTIVITY_TABLES = {
    "transactions": "timestamp",
    "purchases": "created_at",
}

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

def _safe_delete_by(table_name: str, col: str, cutoff_iso: str) -> int:
    try:
        resp = get_table(table_name).delete().lte(col, cutoff_iso).execute()
        data = getattr(resp, "data", None)
        return len(data) if isinstance(data, list) else -1
    except Exception as e:
        print(f"[cleanup] delete error on {table_name}.{col}: {e}")
        return 0

def purge_ephemeral_after(hours: int = 14) -> Dict[str, int]:
    res: Dict[str, int] = {}
    now_iso = _iso(_utc_now())
    cutoff_iso = _iso(_cutoff(hours=hours))
    for tbl in EPHEMERAL_TABLES:
        count = _safe_delete_by(tbl, "expire_at", now_iso)  # لو فيه expire_at
        if count == 0:  # لا صفوف/أو العمود غير موجود
            count = _safe_delete_by(tbl, "created_at", cutoff_iso)
        res[tbl] = max(count, 0)
    return res

def _has_activity_since(user_id: int, since_iso: str) -> bool:
    for tbl, col in ACTIVITY_TABLES.items():
        try:
            r = (get_table(tbl).select("id").eq("user_id", user_id).gte(col, since_iso).limit(1).execute())
            if getattr(r, "data", None):
                return True
        except Exception as e:
            print(f"[cleanup] activity probe error {tbl}.{col} for {user_id}: {e}")
            continue
    return False

def preview_inactive_users(days: int = 33, limit: int = 100_000) -> List[Dict[str, Any]]:
    cutoff_iso = _iso(_cutoff(days=days))
    rows: List[Dict[str, Any]] = []
    try:
        resp = get_table("houssin363").select("user_id, updated_at, created_at").lte("updated_at", cutoff_iso).limit(limit).execute()
        rows = getattr(resp, "data", None) or []
    except Exception:
        try:
            resp = get_table("houssin363").select("user_id, created_at").lte("created_at", cutoff_iso).limit(limit).execute()
            rows = getattr(resp, "data", None) or []
        except Exception as e:
            print(f"[cleanup] select houssin363 failed: {e}")
            return []
    out = []
    for r in rows:
        uid = r.get("user_id")
        if uid is None:
            continue
        if not _has_activity_since(int(uid), cutoff_iso):
            out.append(r)
    return out

def delete_inactive_users(days: int = 33, batch_size: int = 500) -> List[int]:
    candidates = preview_inactive_users(days=days)
    ids = [int(r["user_id"]) for r in candidates if r.get("user_id") is not None]
    if not ids:
        return []
    deleted: List[int] = []
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i+batch_size]
        try:
            get_table("houssin363").delete().in_("user_id", chunk).execute()
            deleted.extend(chunk)
        except Exception as e:
            print(f"[cleanup] delete houssin363 chunk failed: {e}")
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
            print(f"[cleanup] deleted houssin363 users: {len(deleted)}")
    except Exception as e:
        print(f"[cleanup] delete_inactive_users error: {e}")

def schedule_housekeeping(bot=None, every_seconds: int = 3600):
    def _loop():
        _housekeeping_tick(bot)
        threading.Timer(every_seconds, _loop).start()
    threading.Timer(60, _loop).start()
