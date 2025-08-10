# services/state_service.py
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from database.db import get_table

TABLE = "user_state"

# ===================== Helpers =====================

def _ensure_row(user_id: int):
    # upsert صف للمستخدم إن لم يوجد
    get_table(TABLE).upsert(
        {"user_id": user_id, "history": [], "vars": {}},
        on_conflict="user_id"
    ).execute()

def _get_vars(user_id: int) -> Dict[str, Any]:
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    return (res.data[0].get("vars") if res.data else {}) or {}

def _set_vars(user_id: int, vars_dict: Dict[str, Any]):
    _ensure_row(user_id)
    get_table(TABLE).update({"vars": vars_dict}).eq("user_id", user_id).execute()

def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # نقبل 'YYYY-MM-DDTHH:MM:SS' ونتجاهل أجزاء الميلي ثانية/المنطقة وقت المقارنة
        return datetime.fromisoformat(ts[:19])
    except Exception:
        return None

# ===================== History =====================

def append_history(user_id: int, tag: str):
    _ensure_row(user_id)
    res = get_table(TABLE).select("history").eq("user_id", user_id).limit(1).execute()
    hist = (res.data[0].get("history") if res.data else []) or []
    hist.append({"ts": _now_iso(), "tag": tag})
    get_table(TABLE).update({"history": hist}).eq("user_id", user_id).execute()

def get_history(user_id: int):
    res = get_table(TABLE).select("history").eq("user_id", user_id).limit(1).execute()
    return (res.data[0].get("history") if res.data else []) or []

def clear_history(user_id: int):
    _ensure_row(user_id)
    get_table(TABLE).update({"history": []}).eq("user_id", user_id).execute()

# ===================== Generic vars =====================

def set_var(user_id: int, key: str, value: Any):
    vars_dict = _get_vars(user_id)
    vars_dict[key] = value
    _set_vars(user_id, vars_dict)

def get_var(user_id: int, key: str, default=None):
    vars_dict = _get_vars(user_id)
    return vars_dict.get(key, default)

# ===================== Flows (named steps) =====================

def set_step(user_id: int, flow: str, step: str, payload: Optional[Dict[str, Any]] = None):
    vars_dict = _get_vars(user_id)
    flows = vars_dict.get("flows") or {}
    flows[flow] = {"step": step, "payload": payload or {}, "updated_at": _now_iso()}
    vars_dict["flows"] = flows
    _set_vars(user_id, vars_dict)

def get_step(user_id: int, flow: str) -> Dict[str, Any]:
    vars_dict = _get_vars(user_id)
    flows = vars_dict.get("flows") or {}
    return flows.get(flow) or {}

def clear_step(user_id: int, flow: str):
    vars_dict = _get_vars(user_id)
    flows = vars_dict.get("flows") or {}
    if flow in flows:
        flows.pop(flow, None)
        vars_dict["flows"] = flows
        _set_vars(user_id, vars_dict)

# ===================== Compatibility API =====================
# تدعم النمطين:
# 1) قديم: set_state(user_id, key, value)
# 2) جديد: set_state(user_id, value, ttl_minutes=120)  ← هذا المطلوب من state_adapter

def get_state_key(user_id: int, key: str, default=None):
    """أعد القيمة المخزنة في vars[key] أو default إن لم توجد."""
    return get_var(user_id, key, default)

def set_state(*args, **kwargs):
    """
    استعمالان:
      - set_state(user_id, key, value)        → يحفظ في vars[key]
      - set_state(user_id, value, ttl_minutes=120)
        → يحفظ الحالة العامة في vars['__state'] مع انتهاء صلاحية اختياري في vars['__state_exp']
    """
    if not args:
        raise TypeError("set_state: missing arguments")

    # نمط قديم: user_id, key, value
    if len(args) >= 3 and isinstance(args[1], str):
        user_id, key, value = args[0], args[1], args[2]
        set_var(user_id, key, value)
        return

    # نمط جديد: user_id, value, ttl_minutes=...
    if len(args) >= 2:
        user_id, value = args[0], args[1]
        ttl_minutes = kwargs.get("ttl_minutes")
        vars_dict = _get_vars(user_id)
        vars_dict["__state"] = value
        if ttl_minutes and isinstance(ttl_minutes, (int, float)) and ttl_minutes > 0:
            exp = datetime.utcnow() + timedelta(minutes=int(ttl_minutes))
            vars_dict["__state_exp"] = exp.isoformat()
        else:
            # لا يوجد TTL → احذف أي تاريخ سابق
            vars_dict.pop("__state_exp", None)
        _set_vars(user_id, vars_dict)
        return

    raise TypeError("set_state: unsupported arguments signature")

def get_state(user_id: int):
    """
    يعيد الحالة العامة (vars['__state']) إن لم تنتهِ صلاحيتها.
    يرجع None إن كانت منتهية أو غير موجودة.
    """
    vars_dict = _get_vars(user_id)
    val = vars_dict.get("__state")
    exp = _parse_iso(vars_dict.get("__state_exp"))
    if exp and datetime.utcnow() > exp:
        return None
    return val

def clear_state(user_id: int, key: Optional[str] = None):
    """
    لو key=None:
        يحذف الحالة العامة (__state / __state_exp) ولا يلمس باقي vars.
    لو محدد key:
        يحذف المفتاح من vars.
    """
    vars_dict = _get_vars(user_id)
    if key is None:
        vars_dict.pop("__state", None)
        vars_dict.pop("__state_exp", None)
    else:
        vars_dict.pop(key, None)
    _set_vars(user_id, vars_dict)
