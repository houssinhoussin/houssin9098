# services/state_service.py
from datetime import datetime
from typing import Optional, Dict, Any
from database.db import get_table

TABLE = "user_state"

def _ensure_row(user_id: int):
    # upsert صف للمستخدم إن لم يوجد
    get_table(TABLE).upsert({"user_id": user_id, "history": [], "vars": {}}, on_conflict="user_id").execute()

def _get_vars(user_id: int) -> Dict[str, Any]:
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    return (res.data[0].get("vars") if res.data else {}) or {}

def _set_vars(user_id: int, vars_dict: Dict[str, Any]):
    _ensure_row(user_id)
    get_table(TABLE).update({"vars": vars_dict}).eq("user_id", user_id).execute()

# ---------- History helpers ----------
def append_history(user_id: int, tag: str):
    _ensure_row(user_id)
    res = get_table(TABLE).select("history").eq("user_id", user_id).limit(1).execute()
    hist = (res.data[0].get("history") if res.data else []) or []
    hist.append({"ts": datetime.utcnow().isoformat(), "tag": tag})
    get_table(TABLE).update({"history": hist}).eq("user_id", user_id).execute()

def get_history(user_id: int):
    res = get_table(TABLE).select("history").eq("user_id", user_id).limit(1).execute()
    return (res.data[0].get("history") if res.data else []) or []

def clear_history(user_id: int):
    _ensure_row(user_id)
    get_table(TABLE).update({"history": []}).eq("user_id", user_id).execute()

# ---------- Generic vars helpers ----------
def set_var(user_id: int, key: str, value: Any):
    vars_dict = _get_vars(user_id)
    vars_dict[key] = value
    _set_vars(user_id, vars_dict)

def get_var(user_id: int, key: str, default=None):
    vars_dict = _get_vars(user_id)
    return vars_dict.get(key, default)

# ---------- Flows (named steps) ----------
def set_step(user_id: int, flow: str, step: str, payload: Optional[Dict[str, Any]] = None):
    vars_dict = _get_vars(user_id)
    flows = vars_dict.get("flows") or {}
    flows[flow] = {"step": step, "payload": payload or {}, "updated_at": datetime.utcnow().isoformat()}
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

# ---------- Compatibility API for services.state_adapter ----------
# بعض المشاريع القديمة تتوقع دوال بهذه الأسماء:
# get_state_key(user_id, key, default), set_state(user_id, key, value), clear_state(user_id, key=None)

def get_state_key(user_id: int, key: str, default=None):
    """أعد القيمة المخزنة في vars[key] أو default إن لم توجد."""
    return get_var(user_id, key, default)

def set_state(user_id: int, key: str, value: Any):
    """احفظ القيمة في vars[key]."""
    set_var(user_id, key, value)

def clear_state(user_id: int, key: Optional[str] = None):
    """
    لو المحدِّد key None، أفرغ كامل vars.
    وإلا احذف المفتاح المحدد فقط.
    """
    vars_dict = _get_vars(user_id)
    if key is None:
        vars_dict = {}
    else:
        vars_dict.pop(key, None)
    _set_vars(user_id, vars_dict)
