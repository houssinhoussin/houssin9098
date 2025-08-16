# services/quiz_service.py
# خدمة مساعدة للعبة: قراءة الإعدادات/القوالب، إدارة حالة اللاعب، والربط مع Supabase (balance/points)

from __future__ import annotations
import json
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import httpx

from config import SUPABASE_URL, SUPABASE_KEY
from services.state_adapter import UserStateDictLike

# ------------------------ إعدادات المسارات ------------------------
BASE = Path("content/quiz")
SETTINGS_PATH = BASE / "settings.json"
ORDER_PATH = BASE / "templates_order.txt"
TEMPLATES_DIR = BASE / "templates"

# ------------------------ حالة المستخدم (TTL=120 دقيقة) ------------------------
user_quiz_state = UserStateDictLike()   # يخزّن: template_id, stage, q_index, active_msg_id, timer_cancel, started_at

# ------------------------ Supabase REST helpers ------------------------
def _rest_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }

def _table_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"

def sb_select_one(table: str, filters: Dict[str, Any], select: str = "*") -> Optional[Dict[str, Any]]:
    # مثال: filters={"user_id":"eq.123"}
    params = {"select": select}
    params.update(filters)
    with httpx.Client(timeout=20.0) as client:
        r = client.get(_table_url(table), headers=_rest_headers(), params=params)
        r.raise_for_status()
        arr = r.json()
        return arr[0] if arr else None

def sb_upsert(table: str, row: Dict[str, Any], on_conflict: str | None = None) -> Dict[str, Any]:
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    with httpx.Client(timeout=20.0) as client:
        r = client.post(_table_url(table), headers=_rest_headers(), params=params, json=row)
        r.raise_for_status()
        out = r.json()
        return out[0] if isinstance(out, list) and out else row

def sb_update(table: str, filters: Dict[str, Any], patch: Dict[str, Any]) -> List[Dict[str, Any]]:
    params = {}
    params.update(filters)
    with httpx.Client(timeout=20.0) as client:
        r = client.patch(_table_url(table), headers=_rest_headers(), params=params, json=patch)
        r.raise_for_status()
        out = r.json()
        return out if isinstance(out, list) else []

# ------------------------ إعدادات اللعبة ------------------------
_DEFAULT_SETTINGS = {
    "seconds_per_question": 60,
    "timer_tick_seconds": 1,
    "timer_bar_full": "🟩",
    "timer_bar_empty": "⬜",
    "points_per_stars": {"3": 3, "2": 2, "1": 1, "0": 0},
    "points_conversion_rate": {"points_per_unit": 10, "syp_per_unit": 100},  # 10 نقاط = 100 ل.س (عدّلها لاحقًا)
    # شرائح أسعار افتراضية للمرحلة
    "attempt_price_by_stage": [
        {"range": [1, 5],   "price": 25},
        {"range": [6, 10],  "price": 50},
        {"range": [11, 20], "price": 75},
        {"range": [21, 30], "price": 100},
    ],
}

_SETTINGS_CACHE: Dict[str, Any] = {}
_TEMPLATES_CACHE: Dict[str, Dict[str, Any]] = {}

def _safe_json_load(p: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data
    except Exception:
        return fallback.copy()

def load_settings(refresh: bool = False) -> Dict[str, Any]:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE and not refresh:
        return _SETTINGS_CACHE
    if SETTINGS_PATH.exists():
        _SETTINGS_CACHE = _safe_json_load(SETTINGS_PATH, _DEFAULT_SETTINGS)
    else:
        _SETTINGS_CACHE = _DEFAULT_SETTINGS.copy()
    return _SETTINGS_CACHE

def _read_templates_order() -> List[str]:
    if ORDER_PATH.exists():
        ids = [line.strip() for line in ORDER_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        return ids
    return ["T01"]

def get_attempt_price(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    bands = s.get("attempt_price_by_stage") or _DEFAULT_SETTINGS["attempt_price_by_stage"]
    for band in bands:
        lo, hi = band["range"]
        if lo <= stage_no <= hi:
            return int(band["price"])
    return int(bands[-1]["price"])

def get_points_value_syp(points: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    conv = s.get("points_conversion_rate", _DEFAULT_SETTINGS["points_conversion_rate"])
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 100))
    units = points // ppu
    return units * spu

# ------------------------ القوالب (الأسئلة) ------------------------
def load_template(template_id: str, refresh: bool = False) -> Dict[str, Any]:
    global _TEMPLATES_CACHE
    if template_id in _TEMPLATES_CACHE and not refresh:
        return _TEMPLATES_CACHE[template_id]
    path = TEMPLATES_DIR / f"{template_id}.json"
    if not path.exists():
        # افتراضيًا ارجع T01
        path = TEMPLATES_DIR / "T01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _TEMPLATES_CACHE[template_id] = data
    return data

def pick_template_for_user(user_id: int) -> str:
    order = _read_templates_order()
    if not order:
        return "T01"
    idx = user_id % len(order)
    return order[idx]

# ------------------------ محفظة/نقاط في جدول houssin363 ------------------------
def ensure_user_wallet(user_id: int, name: str | None = None) -> Dict[str, Any]:
    row = sb_select_one("houssin363", {"user_id": f"eq.{user_id}"})
    if row:
        return row
    return sb_upsert("houssin363", {"user_id": user_id, "name": name or "", "balance": 0, "points": 0}, on_conflict="user_id")

def get_wallet(user_id: int) -> Tuple[int, int]:
    row = sb_select_one("houssin363", {"user_id": f"eq.{user_id}"}, select="balance,points")
    if not row:
        return (0, 0)
    return int(row.get("balance") or 0), int(row.get("points") or 0)

def add_points(user_id: int, delta: int) -> Tuple[int, int]:
    bal, pts = get_wallet(user_id)
    new_pts = max(0, pts + int(delta))
    rows = sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"points": new_pts})
    return (bal, new_pts)

def change_balance(user_id: int, delta: int) -> Tuple[int, int]:
    bal, pts = get_wallet(user_id)
    new_bal = max(0, bal + int(delta))
    rows = sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"balance": new_bal})
    return (new_bal, pts)

def deduct_fee_for_stage(user_id: int, stage_no: int) -> Tuple[bool, int, int]:
    price = get_attempt_price(stage_no)
    bal, pts = get_wallet(user_id)
    if bal < price:
        return (False, bal, price)
    new_bal, _ = change_balance(user_id, -price)
    return (True, new_bal, price)

def convert_points_to_balance(user_id: int) -> Tuple[int, int, int]:
    """يرجع: (points_before, syp_added, points_after)"""
    bal, pts = get_wallet(user_id)
    syp = get_points_value_syp(pts)
    if syp <= 0:
        return (pts, 0, pts)
    # صفّر النقاط وأضف الرصيد
    sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"points": 0, "balance": bal + syp})
    return (pts, syp, 0)

# ------------------------ إدارة جلسة اللاعب ------------------------
def get_progress(user_id: int) -> Dict[str, Any]:
    st = user_quiz_state.get(user_id, {})
    if not st:
        st = {}
    return st

def reset_progress(user_id: int, template_id: Optional[str] = None) -> Dict[str, Any]:
    t = template_id or pick_template_for_user(user_id)
    state = {
        "template_id": t,
        "stage": 1,
        "q_index": 0,
        "active_msg_id": None,
        "timer_cancel": None,
        "started_at": None,
    }
    user_quiz_state[user_id] = state
    return state

def next_question(user_id: int) -> Tuple[Dict[str, Any], Dict[str, Any], int, int]:
    st = get_progress(user_id)
    if not st:
        st = reset_progress(user_id)
    tpl = load_template(st["template_id"])
    stage_no = int(st.get("stage", 1))
    q_idx = int(st.get("q_index", 0))
    arr = tpl["items_by_stage"][str(stage_no)]
    if q_idx >= len(arr):
        # مرحلة مكتملة → انتقل للمرحلة التالية
        stage_no += 1
        st["stage"] = stage_no
        st["q_index"] = 0
        if str(stage_no) not in tpl["items_by_stage"]:
            # لا يوجد مراحل أخرى في هذا القالب → أعد من البداية (أو بدّل القالب)
            st["stage"] = 1
            stage_no = 1
        arr = tpl["items_by_stage"][str(stage_no)]
        q_idx = 0
    item = arr[q_idx]
    return st, item, stage_no, q_idx

def advance(user_id: int):
    st = get_progress(user_id)
    st["q_index"] = int(st.get("q_index", 0)) + 1
    user_quiz_state[user_id] = st
