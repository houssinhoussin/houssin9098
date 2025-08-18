# services/quiz_service.py
# خدمة مساعدة للعبة: إعدادات، حالة اللاعب، Supabase، عدّادات المرحلة، وحساب جائزة المرحلة كنقاط

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import httpx

from config import SUPABASE_URL, SUPABASE_KEY
from services.state_adapter import UserStateDictLike  # نستخدمه ككاش بالذاكرة فقط

# ------------------------ المسارات ------------------------
BASE = Path("content/quiz")
SETTINGS_PATH = BASE / "settings.json"
ORDER_PATH = BASE / "templates_order.txt"
TEMPLATES_DIR = BASE / "templates"

# ------------------------ كاش الحالة بالذاكرة ------------------------
# يُستخدم كذاكرة محلية سريعة فقط، أمّا التخزين الدائم ففي جدول quiz_progress
user_quiz_state = UserStateDictLike()

# ------------------------ حالة وقتية بالذاكرة (timers/debounce) ------------------------
_user_runtime: dict[int, dict] = {}

def get_runtime(user_id: int) -> dict:
    return _user_runtime.get(user_id, {})

def set_runtime(user_id: int, **kwargs) -> dict:
    r = _user_runtime.get(user_id) or {}
    r.update(kwargs)
    _user_runtime[user_id] = r
    return r

def clear_runtime(user_id: int):
    _user_runtime.pop(user_id, None)

# ------------------------ الإعدادات ------------------------
_DEFAULT_SETTINGS = {
    "seconds_per_question": 60,
    "timer_tick_seconds": 5,
    "timer_bar_full": "🟩",
    "timer_bar_empty": "⬜",
    "points_per_stars": {"3": 3, "2": 2, "1": 1, "0": 0},
    "points_conversion_rate": {"points_per_unit": 10, "syp_per_unit": 5},  # مثال: كل 10 نقاط ≈ 5 ل.س
    "attempt_price_by_stage": [
        {"min": 1, "max": 2, "price": 25},
        {"min": 3, "max": 4, "price": 75},
        {"min": 5, "max": 6, "price": 100},
        {"min": 7, "max": 8, "price": 125},
        {"min": 9, "max": 10, "price": 150},
        {"min": 11, "max": 12, "price": 175},
        {"min": 13, "max": 14, "price": 200},
        {"min": 15, "max": 30, "price": 250},
    ],
}

_SETTINGS_CACHE: dict | None = None
_TEMPLATES_CACHE: dict[str, dict] = {}

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

# ------------------------ تقدم اللاعب في قاعدة البيانات (quiz_progress) ------------------------
def _progress_select(user_id: int) -> Optional[Dict[str, Any]]:
    return sb_select_one("quiz_progress", {"user_id": f"eq.{user_id}"})

def _progress_upsert(user_id: int, st: Dict[str, Any]) -> Dict[str, Any]:
    row = {
        "user_id": user_id,
        "template_id": st.get("template_id", "T01"),
        "stage": int(st.get("stage", 1)),
        "q_index": int(st.get("q_index", 0)),
        "stage_stars": int(st.get("stage_stars", 0)),
        "stage_wrong_attempts": int(st.get("stage_wrong_attempts", 0)),
        "stage_done": int(st.get("stage_done", 0)),
        "last_balance": int(st.get("last_balance", 0)),
        "attempts_on_current": int(st.get("attempts_on_current", 0)),
        "last_click_ts": float(st.get("last_click_ts", 0.0)),
        "paid_key": st.get("paid_key"),
    }
    return sb_upsert("quiz_progress", row, on_conflict="user_id")

def persist_state(user_id: int):
    st = user_quiz_state.get(user_id, {}) or {}
    try:
        _progress_upsert(user_id, st)
    except Exception as e:
        print("quiz_progress upsert failed:", e)

def set_and_persist(user_id: int, st: Dict[str, Any]):
    user_quiz_state[user_id] = st
    persist_state(user_id)

# ------------------------ إعدادات ------------------------
def load_settings(refresh: bool = False) -> Dict[str, Any]:
    global _SETTINGS_CACHE
    if (_SETTINGS_CACHE is not None) and not refresh:
        return _SETTINGS_CACHE
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = dict(_DEFAULT_SETTINGS)
    merged.update(data or {})
    _SETTINGS_CACHE = merged
    return merged

# ------------------------ ترتيب القوالب ------------------------
def _read_templates_order() -> List[str]:
    if not ORDER_PATH.exists():
        return []
    arr = [x.strip() for x in ORDER_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [x for x in arr if x]

# ------------------------ محاسبة/اقتصاد ------------------------
def _band_contains(stage_no: int, band: Dict[str, Any]) -> bool:
    lo = int(band.get("min", 1))
    hi = int(band.get("max", 999))
    return lo <= stage_no <= hi

def get_attempt_price(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    bands = s.get("attempt_price_by_stage") or _DEFAULT_SETTINGS["attempt_price_by_stage"]
    for band in bands:
        if _band_contains(stage_no, band):
            return int(band["price"])
    return int(bands[-1]["price"]) if bands else 250

def get_points_value_syp(points: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    conv = s.get("points_conversion_rate", _DEFAULT_SETTINGS["points_conversion_rate"])
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    if ppu <= 0 or spu <= 0:
        return 0
    # كم ل.س تساوي هذه النقاط
    return (int(points) * spu) // ppu

def _syp_to_points(syp: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    conv = s.get("points_conversion_rate", _DEFAULT_SETTINGS["points_conversion_rate"])
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    if ppu <= 0 or spu <= 0:
        return 0
    # نقاط مقابلة لـ س.س
    return (int(syp) * ppu) // spu

# ------------------------ القوالب ------------------------
def load_template(requested_template_id: str, refresh: bool = False) -> Dict[str, Any]:
    """
    يحمّل قالب الأسئلة. لو القالب المطلوب غير موجود، نختار أول قالب متاح من templates_order.txt
    أو من الملفات الموجودة بالمجلد. التخزين المخبئي يتم بالمُعرّف الفعلي الموجود.
    """
    global _TEMPLATES_CACHE
    order = _read_templates_order()
    real_id = requested_template_id if (TEMPLATES_DIR / f"{requested_template_id}.json").exists() \
              else (order[0] if order else "T01")
    if (real_id in _TEMPLATES_CACHE) and not refresh:
        return _TEMPLATES_CACHE[real_id]
    path = TEMPLATES_DIR / f"{real_id}.json"
    if not path.exists():
        path = TEMPLATES_DIR / "T01.json"
        real_id = "T01"
    data = json.loads(path.read_text(encoding="utf-8"))
    _TEMPLATES_CACHE[real_id] = data
    return data

def pick_template_for_user(user_id: int) -> str:
    order = _read_templates_order()
    if not order:
        return "T01"
    idx = user_id % len(order)
    return order[idx]

# ------------------------ محفظة/نقاط (houssin363) ------------------------
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
    sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"points": new_pts})
    return (bal, new_pts)

def change_balance(user_id: int, delta: int) -> Tuple[int, int]:
    bal, pts = get_wallet(user_id)
    new_bal = max(0, bal + int(delta))
    sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"balance": new_bal})
    return (new_bal, pts)

def deduct_fee_for_stage(user_id: int, stage_no: int) -> Tuple[bool, int, int]:
    price = get_attempt_price(stage_no)
    bal, _ = get_wallet(user_id)
    if bal < price:
        return (False, bal, price)
    new_bal, _ = change_balance(user_id, -price)
    return (True, new_bal, price)

# ------------------------ التقدم (ذاكرة) ------------------------
def get_progress(user_id: int) -> Dict[str, Any]:
    # أولوية: الكاش بالذاكرة
    st = user_quiz_state.get(user_id)
    if st:
        return st
    # حمّل من DB
    row = _progress_select(user_id)
    if row:
        st = {
            "template_id": row.get("template_id") or "T01",
            "stage": int(row.get("stage") or 1),
            "q_index": int(row.get("q_index") or 0),
            "active_msg_id": None,
            "started_at": None,
            "stage_stars": int(row.get("stage_stars") or 0),
            "stage_wrong_attempts": int(row.get("stage_wrong_attempts") or 0),
            "stage_done": int(row.get("stage_done") or 0),
            "last_balance": int(row.get("last_balance") or 0),
            "attempts_on_current": int(row.get("attempts_on_current") or 0),
            "last_click_ts": float(row.get("last_click_ts") or 0.0),
            "paid_key": row.get("paid_key"),
        }
        user_quiz_state[user_id] = st
        return st
    # لا شيء موجود بعد
    return {}

def reset_progress(user_id: int, template_id: Optional[str] = None) -> Dict[str, Any]:
    t = template_id or pick_template_for_user(user_id)
    state = {
        "template_id": t,
        "stage": 1,
        "q_index": 0,
        "active_msg_id": None,
        "started_at": None,
        "stage_stars": 0,
        "stage_wrong_attempts": 0,
        "stage_done": 0,
        "last_balance": 0,
        "attempts_on_current": 0,
        "last_click_ts": 0.0,
        "paid_key": None,
    }
    set_and_persist(user_id, state)
    return state

# ------------------------ أسئلة/مراحل ------------------------
def _timer_bar(remaining: int, settings: Dict[str, Any]) -> str:
    full = settings.get("timer_bar_full", "🟩")
    empty = settings.get("timer_bar_empty", "⬜")
    total = 10
    # نسبة الوقت المتبقي من قيمة المؤقت
    ratio = remaining / max(1, int(settings.get("seconds_per_question", 60)))
    filled = max(0, min(total, int(round(ratio * total))))
    return full * filled + empty * (total - filled)

def _question_id(tpl_id: str, stage_no: int, item: dict, q_idx: int) -> str:
    qid = str(item.get("id", q_idx))
    return f"{tpl_id}:{stage_no}:{qid}"

def ensure_paid_before_show(user_id: int) -> Tuple[bool, int, int, str]:
    """
    يحاول خصم سعر المرحلة مرّة واحدة قبل عرض السؤال الحالي.
    يرجع: (ok, balance_or_new_balance, price, reason)
      - ok=True و reason in {"already","paid","no-questions"}
      - ok=False و reason="insufficient"
    """
    # تأكد من وجود محفظة للمستخدم
    ensure_user_wallet(user_id)

    st = get_progress(user_id) or reset_progress(user_id)
    tpl_id = st.get("template_id", "T01")
    tpl = load_template(tpl_id)
    stage_no = int(st.get("stage", 1))
    q_idx = int(st.get("q_index", 0))
    items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
    if not items:
        return (True, st.get("last_balance", 0), 0, "no-questions")

    # خصم لمرة واحدة لكل سؤال
    if st.get("paid_key") == _question_id(tpl_id, stage_no, items[min(q_idx, len(items)-1)], q_idx):
        return (True, st.get("last_balance", 0), get_attempt_price(stage_no), "already")

    ok, new_bal, price = deduct_fee_for_stage(user_id, stage_no)
    if not ok:
        return (False, new_bal, price, "insufficient")

    st["last_balance"] = new_bal
    st["paid_key"] = _question_id(tpl_id, stage_no, items[min(q_idx, len(items)-1)], q_idx)
    set_and_persist(user_id, st)
    return (True, new_bal, price, "paid")

def next_question(user_id: int) -> Tuple[Dict[str, Any], dict, int, int]:
    st = get_progress(user_id) or reset_progress(user_id)
    tpl = load_template(st.get("template_id", "T01"))
    stage_no = int(st.get("stage", 1))
    q_idx = int(st.get("q_index", 0))
    arr = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []

    if not arr:
        dummy = {"id": "EMPTY", "text": "لا توجد أسئلة لهذه المرحلة.", "options": ["-"], "correct_index": 0}
        return st, dummy, stage_no, 0

    if q_idx >= len(arr):
        q_idx = len(arr) - 1  # clamp
    item = arr[q_idx]
    return st, item, stage_no, q_idx

def advance(user_id: int):
    st = get_progress(user_id)
    st["q_index"] = int(st.get("q_index", 0)) + 1
    # سؤال جديد ⇒ إزالة مفتاح الدفع لضمان خصم جديد
    st.pop("paid_key", None)
    set_and_persist(user_id, st)

# ------------------------ منطق المرحلة والجوائز (كنقاط) ------------------------
def stage_question_count(stage_no: int) -> int:
    # م1–2: 20 سؤال، ثم +5 كل مرحلة
    return 20 if stage_no <= 2 else 20 + (stage_no - 2) * 5

def _get_stage_counters(user_id: int) -> Tuple[int, int, int]:
    st = get_progress(user_id)
    return int(st.get("stage_stars", 0)), int(st.get("stage_wrong_attempts", 0)), int(st.get("stage_done", 0))

def _reset_stage_counters(user_id: int):
    st = get_progress(user_id)
    st["stage_stars"] = 0
    st["stage_wrong_attempts"] = 0
    st["stage_done"] = 0
    set_and_persist(user_id, st)

def _compute_reward_syp(stars: int, questions: int, stage_no: int, settings: dict) -> int:
    # مثال بسيط: لكل نجمة × وزن، مضروب في عامل المرحلة، سقف ناعم من الاقتصاد
    pts_per_star = settings.get("points_per_stars", _DEFAULT_SETTINGS["points_per_stars"])
    base_points = int(pts_per_star.get(str(max(0, min(3, stars))), 0))
    # قيمة النقاط بالليرة
    value_syp = get_points_value_syp(base_points, settings)
    # عامل مرحلة بسيط
    factor = 1.0 + (max(1, stage_no) - 1) * 0.1
    syp = int(round(value_syp * factor))
    # سقوف اقتصادية اختيارية
    econ = settings.get("economy", {})
    soft_cap_ratio = float(econ.get("op_payout_soft_cap_ratio", 0.0))
    op_free = int(econ.get("op_free_balance", 0))
    if op_free and soft_cap_ratio:
        syp = min(syp, int(op_free * soft_cap_ratio))
    return max(0, syp)

def compute_stage_reward_and_finalize(user_id: int, stage_no: int, questions: int) -> dict:
    """
    يحسب مكافأة المرحلة كنقاط، يضيفها لمحفظة النقاط، ثم يضبط المرحلة التالية ويصفر عدادات المرحلة.
    يرجع: {questions, wrong_attempts, stars, reward_points, points_after}
    """
    settings = load_settings()
    stars, wrongs, done = _get_stage_counters(user_id)
    total_q = questions if questions > 0 else done
    # لو ما خلّص كل أسئلة المرحلة، لا نمنح مكافأة
    if done < total_q:
        _, pts_now = get_wallet(user_id)
        return {"questions": done, "wrong_attempts": wrongs, "stars": stars, "reward_points": 0, "points_after": pts_now}

    # احسب مكافأة بالليرة → حوّلها لنقاط
    reward_syp = _compute_reward_syp(stars, total_q, stage_no, settings)
    reward_points = _syp_to_points(reward_syp, settings) if reward_syp > 0 else 0

    # أضِف النقاط
    _, pts_after_add = add_points(user_id, reward_points)

    # تقدّم المرحلة
    st = get_progress(user_id)
    st["stage"] = int(st.get("stage", 1)) + 1
    st["q_index"] = 0
    # بداية مرحلة جديدة ⇒ إزالة paid_key لضمان خصم أول سؤال في المرحلة
    st.pop("paid_key", None)
    set_and_persist(user_id, st)

    # [PATCH] منح جوائز إضافية عند إكمال القالب أو بعد مرحلة محددة (لا تغييرات على الواجهة):
    try:
        st_now = get_progress(user_id) or {}
        tpl_id = st_now.get("template_id", "T01")
        _settings = load_settings()
        _after_stage = int((_settings.get("rewards") or {}).get("top3_after_stage", 10))
        if int(stage_no) == _after_stage:
            _bonus = payout_on_template_complete(user_id, tpl_id)
            _top3 = _maybe_top3_award_on_stage10(user_id, tpl_id, int(stage_no))
            try:
                # خزّن أثرًا خفيفًا داخل جدول تشغيل المرحلة
                sb_upsert("quiz_stage_runs", {
                    "user_id": user_id,
                    "template_id": tpl_id,
                    "stage_no": stage_no,
                    "bonus_points": int((_bonus or {}).get("award_points", 0)),
                    "top3_award_points": int((_top3 or {}).get("points", 0))
                })
            except Exception:
                pass
    except Exception:
        pass

    _reset_stage_counters(user_id)

    return {
        "questions": int(total_q),
        "wrong_attempts": int(wrongs),
        "stars": int(stars),
        "reward_points": int(reward_points),
        "points_after": int(pts_after_add),
    }

# ------------------------ عدّادات المرحلة أثناء اللعب ------------------------
def register_wrong_attempt(user_id: int):
    st = get_progress(user_id)
    st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
    st["stage_done"] = int(st.get("stage_done", 0)) + 1
    set_and_persist(user_id, st)

def register_correct_answer(user_id: int):
    st = get_progress(user_id)
    st["stage_stars"] = int(st.get("stage_stars", 0)) + 1
    st["stage_done"] = int(st.get("stage_done", 0)) + 1
    set_and_persist(user_id, st)

# ==== [PATCH] إضافات ميزات الجوائز والاقتصاد (بدون تغييرات على الواجهة) ====
def get_stage_time(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    """
    وقت المرحلة الاختياري من الإعدادات (timer.stage_time_s: { "1-2": 60, "3-5": 50, "6+": 45 }).
    إن لم يوجد يرجع seconds_per_question الأساسي.
    """
    s = settings or load_settings()
    timer = (s or {}).get("timer", {})
    stage_time_obj = (timer or {}).get("stage_time_s", {})
    if not stage_time_obj:
        return int((s or {}).get("seconds_per_question", 60))
    def _match(band: str) -> bool:
        if "-" in band:
            lo, hi = band.split("-", 1)
            return int(lo) <= stage_no <= int(hi)
        if band.endswith("+"):
            return stage_no >= int(band[:-1])
        return False
    for band, secs in stage_time_obj.items():
        if _match(band):
            try:
                return int(secs)
            except Exception:
                continue
    return int((s or {}).get("seconds_per_question", 60))

def convert_points_to_balance(user_id: int):
    """
    تحويل وحدات نقاط → رصيد حسب points_conversion_rate.
    يرجع (points_before, syp_added, points_after).
    """
    s = load_settings()
    conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    bal, pts = get_wallet(user_id)
    if ppu <= 0 or spu <= 0:
        return pts, 0, pts
    units = pts // ppu
    if units <= 0:
        return pts, 0, pts
    pts_spent = units * ppu
    syp_add = units * spu
    # خصم نقاط + إضافة رصيد
    add_points(user_id, -pts_spent)
    change_balance(user_id, syp_add)
    _, pts_after = get_wallet(user_id)
    try:
        sb_upsert("transactions", {
            "user_id": user_id,
            "kind": "convert_points_to_balance",
            "payload": json.dumps({"units": units, "points_spent": pts_spent, "syp_added": syp_add}, ensure_ascii=False)
        })
    except Exception:
        pass
    return pts, syp_add, pts_after

def payout_on_template_complete(user_id: int, template_id: str) -> Dict[str, Any]:
    """
    يمنح جائزة إكمال القالب وفق settings.completion_award (ليرة → تُحوّل نقاط).
    يرجع {"award_points": int, "award_syp": int}
    """
    out = {"award_points": 0, "award_syp": 0}
    try:
        s = load_settings()
        comp = s.get("completion_award", {})
        base_syp = int(comp.get("base_award_syp", 0))
        max_syp = int(comp.get("max_award_syp", base_syp))
        econ = s.get("economy", {})
        op_free = int(econ.get("op_free_balance", 0))
        soft_ratio = float(comp.get("soft_cap_ratio_of_op", 0.0))
        if op_free and soft_ratio:
            base_syp = min(base_syp, int(op_free * soft_ratio))
        base_syp = min(base_syp, max_syp)
        if base_syp <= 0:
            return out
        conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
        pts = (base_syp * int(conv.get("points_per_unit", 10))) // max(1, int(conv.get("syp_per_unit", 5)))
        if pts > 0:
            add_points(user_id, int(pts))
        try:
            sb_upsert("quiz_templates_completed", {
                "user_id": user_id,
                "template_id": template_id,
                "payload": json.dumps({"award_points": int(pts), "award_syp": int(base_syp)}, ensure_ascii=False)
            })
        except Exception:
            pass
        out.update({"award_points": int(pts), "award_syp": int(base_syp)})
    except Exception:
        pass
    return out

def _maybe_top3_award_on_stage10(user_id: int, template_id: str, stage_no: int) -> Dict[str, Any]:
    """
    إذا كانت هذه نهاية المرحلة المحددة (افتراضيًا 10) يمنح جوائز Top3.
    يرجع {"rank": 1..3 | None, "points": int}
    """
    out = {"rank": None, "points": 0}
    try:
        s = load_settings()
        rewards = s.get("rewards", {})
        after_stage = int(rewards.get("top3_after_stage", 10))
        if int(stage_no) != after_stage:
            return out
        ratios = rewards.get("top3_awards_ratio_of_op", [])
        maxes = rewards.get("top3_awards_max_syp", [])
        econ = s.get("economy", {})
        op_free = int(econ.get("op_free_balance", 0))
        if not ratios or not maxes or not op_free:
            return out
        # رتّب اللاعبين حسب مجموع نقاطهم في هذا القالب داخل جدول quiz_stage_runs
        try:
            with httpx.Client(timeout=20.0) as client:
                url = _table_url("quiz_stage_runs")
                headers = _rest_headers()
                params = {"select": "user_id,stage_points,template_id", "template_id": f"eq.{template_id}"}
                r = client.get(url, headers=headers, params=params); r.raise_for_status()
                arr = r.json() or []
        except Exception:
            arr = []
        totals = {}
        for row in arr:
            uid = int(row.get("user_id"))
            if row.get("template_id") != template_id:
                continue
            totals[uid] = totals.get(uid, 0) + int(row.get("stage_points") or 0)
        if not totals:
            return out
        ranking = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        rank = None
        for i,(uid, _) in enumerate(ranking, start=1):
            if uid == user_id:
                rank = i; break
        if not rank or rank > 3:
            return out
        i = rank - 1
        syp_award = int(min(op_free * float(ratios[i]), int(maxes[i])))
        conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
        pts = (syp_award * int(conv.get("points_per_unit", 10))) // max(1, int(conv.get("syp_per_unit", 5)))
        if pts > 0:
            add_points(user_id, int(pts))
        try:
            sb_upsert("transactions", {
                "user_id": user_id,
                "kind": "top3_award",
                "payload": json.dumps({"rank": rank, "points": int(pts), "syp": int(syp_award), "template_id": template_id}, ensure_ascii=False)
            })
        except Exception:
            pass
        out.update({"rank": rank, "points": int(pts)})
    except Exception:
        pass
    return out

def get_leaderboard_top(n: int = 10) -> list[dict]:
    """
    قائمة أعلى المستخدمين حسب النقاط من جدول houssin363.
    """
    n = int(max(1, min(100, n)))
    try:
        with httpx.Client(timeout=20.0) as client:
            url = _table_url("houssin363")
            headers = _rest_headers()
            params = {"select": "user_id,name,points,balance", "order": "points.desc", "limit": str(n)}
            r = client.get(url, headers=headers, params=params); r.raise_for_status()
            return r.json() or []
    except Exception:
        return []

# ==== [PATCH-2] لوحة ترتيب بالتقدّم (stage, stage_done) ====
def get_leaderboard_by_progress(n: int = 10) -> list[dict]:
    """
    يُرجع أعلى اللاعبين حسب التقدّم: المرحلة ثم عدد الأسئلة المنجزة في المرحلة.
    البنية: [{"user_id": int, "name": str, "stage": int, "stage_done": int, "points": int, "balance": int}]
    """
    n = int(max(1, min(100, n)))
    rows = []
    try:
        with httpx.Client(timeout=20.0) as client:
            url = _table_url("quiz_progress")
            headers = _rest_headers()
            params = {"select": "user_id,stage,stage_done", "order": "stage.desc,stage_done.desc", "limit": str(n)}
            r = client.get(url, headers=headers, params=params); r.raise_for_status()
            rows = r.json() or []
    except Exception:
        rows = rows or []
    out = []
    for r in rows:
        uid = int(r.get("user_id"))
        try:
            wallet = sb_select_one("houssin363", {"user_id": f"eq.{uid}"}, select="name,points,balance")
        except Exception:
            wallet = None
        out.append({
            "user_id": uid,
            "name": (wallet or {}).get("name") or f"UID{uid}",
            "points": int((wallet or {}).get("points") or 0),
            "balance": int((wallet or {}).get("balance") or 0),
            "stage": int(r.get("stage") or 0),
            "stage_done": int(r.get("stage_done") or 0),
        })
    return out

# توافق مع نسخة "الخطأ": دوال لا-أثر
def seen_clear_user(user_id: int):
    return True

def mark_seen_after_payment(user_id: int):
    return True
