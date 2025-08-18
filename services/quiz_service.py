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

# ------------------------ إعدادات اللعبة ------------------------
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
        {"min": 15, "max": 16, "price": 225},
        {"min": 17, "max": 999, "price": 250},
    ],
    # سياسة الجوائز: نحسب إيرادًا متوقعًا ثم نوزع نسبة منه حسب الأداء
    "reward_policy": {
        "target_payout_ratio": 0.30,
        "bands": [
            {"name": "high", "stars_pct_min": 0.70, "payout_ratio": 1.00},
            {"name": "mid",  "stars_pct_min": 0.50, "payout_ratio": 0.60},
            {"name": "low",  "stars_pct_min": 0.33, "payout_ratio": 0.25},
        ]
    },
}

_SETTINGS_CACHE: Dict[str, Any] = {}
_TEMPLATES_CACHE: Dict[str, Dict[str, Any]] = {}

def _safe_json_load(p: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
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

# --- قراءة ترتيب القوالب مع تصفية ما لا يوجد فعليًا ---
def _read_templates_order() -> List[str]:
    existing = {p.stem for p in TEMPLATES_DIR.glob("T*.json")}
    if ORDER_PATH.exists():
        order = [ln.strip() for ln in ORDER_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
        order = [t for t in order if t in existing]
        if order:
            return order
    if existing:
        return sorted(existing)
    return ["T01"]

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
    units = points // ppu
    return units * spu

def _syp_to_points(amount_syp: int, settings: Dict[str, Any]) -> int:
    conv = settings.get("points_conversion_rate", _DEFAULT_SETTINGS["points_conversion_rate"])
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    if spu <= 0:
        return 0
    units = amount_syp // spu
    return units * ppu

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

# ------------------------ خصم آمن قبل عرض السؤال ------------------------
def _current_q_key(user_id: int, tpl_id: str, stage_no: int, q_idx: int, item: Dict[str, Any]) -> str:
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
    arr = (tpl.get("items_by_stage", {}) or {}).get(str(stage_no), []) or []
    if not arr:
        bal, _ = get_wallet(user_id)
        return True, bal, 0, "no-questions"

    if q_idx >= len(arr):
        q_idx = len(arr) - 1

    item = arr[q_idx]
    q_key = _current_q_key(user_id, tpl_id, stage_no, q_idx, item)

    # لو هذا السؤال مدفوع سابقًا لنفس النسخة فلا نكرر الخصم
    if st.get("paid_key") == q_key:
        bal, _ = get_wallet(user_id)
        return True, bal, 0, "already"

    ok, new_bal_or_old, price = deduct_fee_for_stage(user_id, stage_no)
    if not ok:
        st["last_balance"] = new_bal_or_old
        set_and_persist(user_id, st)
        return False, new_bal_or_old, price, "insufficient"

    # تمييز هذه النسخة من السؤال كمدفوعة
    st["paid_key"] = q_key
    st["last_balance"] = new_bal_or_old
    set_and_persist(user_id, st)
    return True, new_bal_or_old, price, "paid"

def pause_current_question(user_id: int) -> None:
    """استدعِها عندما يضغط اللاعب 'أكمل لاحقًا' لفرض خصم جديد عند الاستئناف."""
    st = get_progress(user_id)
    if st:
        st.pop("paid_key", None)
        set_and_persist(user_id, st)

# ------------------------ جلسة اللاعب (قراءة/كتابة دائمة عبر quiz_progress) ------------------------
def get_progress(user_id: int) -> Dict[str, Any]:
    # إن كان في الكاش أعِده
    st = user_quiz_state.get(user_id)
    if st:
        return st
    # حمّل من قاعدة البيانات
    row = _progress_select(user_id)
    if row:
        st = {
            "template_id": row.get("template_id", "T01"),
            "stage": int(row.get("stage", 1)),
            "q_index": int(row.get("q_index", 0)),
            "active_msg_id": None,
            "started_at": None,
            "stage_stars": int(row.get("stage_stars", 0)),
            "stage_wrong_attempts": int(row.get("stage_wrong_attempts", 0)),
            "stage_done": int(row.get("stage_done", 0)),
            "last_balance": int(row.get("last_balance", 0)),
            "attempts_on_current": int(row.get("attempts_on_current", 0)),
            "last_click_ts": float(row.get("last_click_ts", 0.0)),
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

def _tpl_items_for_stage(tpl: Dict[str, Any], stage_no: int) -> List[Dict[str, Any]]:
    return (tpl.get("items_by_stage", {}) or {}).get(str(stage_no), []) or []

def next_question(user_id: int) -> Tuple[Dict[str, Any], Dict[str, Any], int, int]:
    st = get_progress(user_id)
    if not st:
        st = reset_progress(user_id)
    tpl = load_template(st["template_id"])
    stage_no = int(st.get("stage", 1))
    q_idx = int(st.get("q_index", 0))
    arr = _tpl_items_for_stage(tpl, stage_no)

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
    st["attempts_on_current"] = 0
    set_and_persist(user_id, st)

def _compute_reward_syp(stars: int, questions: int, stage_no: int, settings: dict) -> int:
    # إيراد متوقع ≈ 2.5 محاولة/سؤال
    price = get_attempt_price(stage_no, settings)
    expected_R = 2.5 * questions * price
    pol = settings.get("reward_policy", _DEFAULT_SETTINGS["reward_policy"])
    max_payout = float(pol.get("target_payout_ratio", 0.30)) * expected_R
    bands = pol.get("bands", [])
    stars_pct = 0.0 if questions <= 0 else (float(stars) / (3.0 * questions))
    chosen = None
    for b in sorted(bands, key=lambda x: float(x.get("stars_pct_min", 0.0)), reverse=True):
        if stars_pct >= float(b.get("stars_pct_min", 0.0)):
            chosen = b
            break
    if not chosen:
        return 0
    return int(round(max_payout * float(chosen.get("payout_ratio", 0.0))))

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
    if reward_points > 0:
        _, pts_after = add_points(user_id, reward_points)
    else:
        _, pts_after = get_wallet(user_id)

    # لوج المرحلة في قاعدة البيانات (يتضمن template_id)
    st = get_progress(user_id)
    payload = {
        "user_id": user_id,
        "template_id": st.get("template_id", "T01"),
        "stage_no": stage_no,
        "questions": int(total_q),
        "stars": int(stars),
        "wrong_attempts": int(wrongs),
        "reward_points": int(reward_points),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        sb_upsert("quiz_stage_runs", payload)
    except Exception as e:
        print("quiz_stage_runs insert failed:", e, payload)

    # جهّز المرحلة التالية ضمن نفس القالب أو أعد للأولى إذا انتهت
    tpl = load_template(st["template_id"])
    next_stage = stage_no + 1
    if str(next_stage) in (tpl.get("items_by_stage", {}) or {}):
        st["stage"] = next_stage
    else:
        st["stage"] = 1
    st["q_index"] = 0
    # بداية مرحلة جديدة ⇒ إزالة paid_key لضمان خصم أول سؤال في المرحلة
    st.pop("paid_key", None)
    set_and_persist(user_id, st)

    _reset_stage_counters(user_id)

    return {
        "questions": int(total_q),
        "wrong_attempts": int(wrongs),
        "stars": int(stars),
        "reward_points": int(reward_points),
        "points_after": int(pts_after),
    }


# ====== Injected Helpers (per spec) ======

def get_seconds_for_stage(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    table = s.get("seconds_by_stage") or []
    for band in table:
        try:
            lo = int(band.get("min",1)); hi = int(band.get("max",999)); sec = int(band.get("seconds", 60))
            if lo <= stage_no <= hi:
                return sec
        except Exception:
            continue
    return int(s.get("seconds_per_question", 60))

def convert_points_to_balance(user_id: int, syp_per_point: float | None = None, consume_all: bool = True) -> tuple[int,int,int]:
    """يحوّل نقاط اللاعب إلى رصيد. يرجع (pts_before, syp_added, pts_after)."""
    bal, pts = get_wallet(user_id)
    if pts <= 0:
        return (0, 0, 0)
    settings = load_settings()
    if syp_per_point is None:
        conv = settings.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
        ppu = max(1, int(conv.get("points_per_unit", 10)))
        spu = max(0, int(conv.get("syp_per_unit", 5)))
        syp_per_point = (spu / ppu) if ppu > 0 else 0.0
    pts_before = int(pts)
    if consume_all:
        syp_added = int(round(pts_before * float(syp_per_point)))
        pts_after = 0
    else:
        conv = settings.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
        ppu = max(1, int(conv.get("points_per_unit", 10)))
        spu = max(0, int(conv.get("syp_per_unit", 5)))
        units = pts_before // ppu
        syp_added = int(units * spu)
        pts_after = int(pts_before - units * ppu)
    if syp_added > 0:
        change_balance(user_id, syp_added)
    sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"points": int(pts_after)})
    return (int(pts_before), int(syp_added), int(pts_after))

def set_flag(user_id: int, key: str, value: Any):
    st = get_progress(user_id) or reset_progress(user_id)
    st[key] = value
    set_and_persist(user_id, st)

def get_flag(user_id: int, key: str, default=None):
    st = get_progress(user_id)
    if not st:
        return default
    return st.get(key, default)

def must_convert_now(user_id: int) -> bool:
    st = get_progress(user_id) or {}
    stage = int(st.get("stage", 1))
    if stage >= 3:
        return bool(st.get("must_convert_after_stage2", False))
    return False
