# services/state_service.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from database.db import table

TABLE = "user_state"

def _now_utc():
    return datetime.now(timezone.utc)

def _iso(dt: datetime) -> str:
    return dt.isoformat()

def set_state(user_id: int, state_key: str,
              ttl_minutes: Optional[int] = 120,
              extra: Optional[Dict[str, Any]] = None):
    """حفظ حالة المستخدم (سجل واحد لكل user_id). Upsert على user_id."""
    payload = {
        "user_id": user_id,
        "state_key": state_key,
        "state": extra or {},
        "updated_at": _iso(_now_utc()),
    }
    if ttl_minutes:
        payload["expires_at"] = _iso(_now_utc() + timedelta(minutes=ttl_minutes))
    # unique على user_id → on_conflict="user_id"
    table(TABLE).upsert(payload, on_conflict="user_id").execute()

def get_state_key(user_id: int, default: Optional[str] = None) -> Optional[str]:
    """إرجاع اسم الحالة الحالية أو default إن لم توجد/انتهت."""
    res = table(TABLE).select("state_key, expires_at").eq("user_id", user_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        return default
    row = rows[0]
    exp = row.get("expires_at")
    try:
        if exp:
            # من ISO إلى datetime مع دعم Z
            if isinstance(exp, str):
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            else:
                exp_dt = exp  # أحيانًا supabase يعيدها datetime جاهز
            if exp_dt < _now_utc():
                return default
    except Exception:
        pass
    return row.get("state_key") or default

def set_state_data(user_id: int, data: Dict[str, Any]):
    table(TABLE).update({"state": data}).eq("user_id", user_id).execute()

def get_state_data(user_id: int) -> Dict[str, Any]:
    res = table(TABLE).select("state").eq("user_id", user_id).limit(1).execute()
    rows = res.data or []
    return rows[0].get("state") if rows else {}

def clear_state(user_id: int):
    table(TABLE).delete().eq("user_id", user_id).execute()
