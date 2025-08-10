# services/state_service.py
from datetime import datetime
from typing import Optional, Dict, Any
from database.db import get_table

TABLE = "user_state"

def _ensure_row(user_id: int):
    # upsert صف للمستخدم إن لم يوجد
    get_table(TABLE).upsert({"user_id": user_id, "history": [], "vars": {}}, on_conflict="user_id").execute()

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

def set_var(user_id: int, key: str, value: Any):
    _ensure_row(user_id)
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    vars = (res.data[0].get("vars") if res.data else {}) or {}
    vars[key] = value
    get_table(TABLE).update({"vars": vars}).eq("user_id", user_id).execute()

def get_var(user_id: int, key: str, default=None):
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    vars = (res.data[0].get("vars") if res.data else {}) or {}
    return vars.get(key, default)

def set_step(user_id: int, flow: str, step: str, payload: Optional[Dict[str, Any]] = None):
    _ensure_row(user_id)
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    vars = (res.data[0].get("vars") if res.data else {}) or {}
    flows = vars.get("flows") or {}
    flows[flow] = {"step": step, "payload": payload or {}, "updated_at": datetime.utcnow().isoformat()}
    vars["flows"] = flows
    get_table(TABLE).update({"vars": vars}).eq("user_id", user_id).execute()

def get_step(user_id: int, flow: str) -> Dict[str, Any]:
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    vars = (res.data[0].get("vars") if res.data else {}) or {}
    flows = vars.get("flows") or {}
    return flows.get(flow) or {}

def clear_step(user_id: int, flow: str):
    _ensure_row(user_id)
    res = get_table(TABLE).select("vars").eq("user_id", user_id).limit(1).execute()
    vars = (res.data[0].get("vars") if res.data else {}) or {}
    flows = vars.get("flows") or {}
    if flow in flows:
        flows.pop(flow, None)
        vars["flows"] = flows
        get_table(TABLE).update({"vars": vars}).eq("user_id", user_id).execute()
