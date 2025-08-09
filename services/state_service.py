# services/state_service.py
from __future__ import annotations
import datetime as dt
import logging
from typing import Any, Optional, Dict

import httpx
from database.db import get_table
from config import TABLE_USER_STATE as _TABLE_USER_STATE  # إن لم يوجد ستقع للبديل أدناه
from utils.retry import retry

# اسم الجدول من config أو الافتراضي
STATE_TABLE = _TABLE_USER_STATE or "user_state"

def _now_utc() -> dt.datetime:
    # نُعيد datetime واعٍ بالمنطقة (UTC)
    return dt.datetime.now(tz=dt.timezone.utc)

def _parse_iso(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        # يدعم "YYYY-MM-DDTHH:MM:SS[.fff][±offset]" أو بدون offset
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        # لو نايف (بدون tzinfo) اعتبره UTC
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None

def _to_ts(secs: Optional[int]) -> Optional[str]:
    if not secs:
        return None
    return (_now_utc() + dt.timedelta(seconds=secs)).isoformat()

@retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="upsert user state")
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

@retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="select user state")
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
    exp_raw = row.get("expires_at")
    if exp_raw:
        exp_dt = _parse_iso(exp_raw)
        if exp_dt:
            if exp_dt < _now_utc():
                try:
                    delete_state(user_id, key)
                except Exception:
                    logging.warning("Failed to delete expired state row; ignoring.")
                return {}
        else:
            logging.warning("Invalid expires_at on state row; ignoring.")

    return row.get("state") or {}

@retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="delete user state")
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

@retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="clear user states")
def clear_user(user_id: int):
    """
    حذف جميع حالات المستخدم (تنظيف بعد إنهاء العميل).
    """
    return get_table(STATE_TABLE).delete().eq("user_id", user_id).execute()

@retry((httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, Exception), what="cleanup expired states")
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
