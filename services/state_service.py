# services/state_service.py
from __future__ import annotations
import datetime as dt
from typing import Any, Optional, Dict
from database.db import get_table
import logging

STATE_TABLE = "user_state"

def _now_utc() -> dt.datetime:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def _to_ts(secs: Optional[int]) -> Optional[str]:
    if not secs:
        return None
    return (_now_utc() + dt.timedelta(seconds=secs)).isoformat()

def set_state(user_id: int, key: str, value: Dict[str, Any], ttl_seconds: Optional[int] = None):
    """
    يحفظ/يحدث حالة مستخدم (UPSERT) لقيمة JSON، مع خيار مدة صلاحية (TTL).
    """
    payload = {
        "user_id": user_id,
        "state_key": key,
        "state": value,
        "updated_at": _now_utc().isoformat()
    }
    exp = _to_ts(ttl_seconds)
    if exp:
        payload["expires_at"] = exp

    # upsert via on_conflict (user_id, state_key)
    return (
        get_table(STATE_TABLE)
        .upsert(payload, on_conflict="user_id,state_key")
        .execute()
    )

def get_state(user_id: int, key: str) -> Dict[str, Any]:
    """
    يرجع JSON الحالة (أو {} لو غير موجود، أو منتهي صلاحية)
    """
    res = (
        get_table(STATE_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("state_key", key)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return {}
    row = rows[0]
    # تحقق من الانقضاء
    exp = row.get("expires_at")
    if exp:
        try:
            if dt.datetime.fromisoformat(exp) < _now_utc():
                # منتهي → احذفه وارجع {}
                delete_state(user_id, key)
                return {}
        except Exception:
            logging.warning("Invalid expires_at on state row; ignoring.")
    return row.get("state") or {}

def delete_state(user_id: int, key: str):
    """
    حذف حالة مفتاح واحد للمستخدم.
    """
    return (
        get_table(STATE_TABLE)
        .delete()
        .eq("user_id", user_id)
        .eq("state_key", key)
        .execute()
    )

def clear_user(user_id: int):
    """
    حذف جميع حالات المستخدم (تنظيف بعد إنهاء العميل).
    """
    return get_table(STATE_TABLE).delete().eq("user_id", user_id).execute()

def cleanup_expired_states():
    """
    تنظيف عام: حذف الحالات المنتهية الصلاحية.
    """
    now_iso = _now_utc().isoformat()
    return (
        get_table(STATE_TABLE)
        .delete()
        .lt("expires_at", now_iso)
        .execute()
    )

# واجهات مساعدة شائعة الاستخدام:
FLOW_KEY = "flow_step"
CART_KEY = "cart"

def set_flow_step(user_id: int, step: str, ttl_seconds: Optional[int] = 3600):
    return set_state(user_id, FLOW_KEY, {"step": step}, ttl_seconds)

def get_flow_step(user_id: int) -> str:
    st = get_state(user_id, FLOW_KEY)
    return st.get("step") or ""

def clear_flow(user_id: int):
    return delete_state(user_id, FLOW_KEY)

def set_cart(user_id: int, cart: Dict[str, Any], ttl_seconds: Optional[int] = 86400):
    return set_state(user_id, CART_KEY, cart, ttl_seconds)

def get_cart(user_id: int) -> Dict[str, Any]:
    return get_state(user_id, CART_KEY)

def clear_cart(user_id: int):
    return delete_state(user_id, CART_KEY)
