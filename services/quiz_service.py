# services/quiz_service.py
# خدمة مساعدة للعبة: قراءة الإعدادات/القوالب، إدارة حالة اللاعب، والربط مع Supabase (balance/points)
# ✅ محدثة لإضافة:
#   - stage_question_count: م1–2=20 ثم +5 كل مرحلة
#   - عدّادات المرحلة: نجوم/محاولات خاطئة/عدد منجَز
#   - compute_stage_reward_and_finalize: حساب الجائزة وإيداعها وتسجيل الملخّص
#   - runtime (timer/cancel) في ذاكرة محلية فقط (لا تُحفظ في Supabase) لمنع خطأ JSON
#   - get_attempt_price يدعم شكلين: {"range":[lo,hi],"price"} و {"min":..,"max":..,"price"}

from __future__ import annotations
import json
import time
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
# ❗ نحفظ فقط أشياء قابلة للتسلسل JSON هنا (لا نضع threading.Event أو كائنات)
user_quiz_state = UserStateDictLike()   # يخزّن: template_id, stage, q_index, active_msg_id, ... (قيم بدائية فقط)

# ✳️ حالة وقتية لا تُحفَظ في Supabase (Timer, etc.) حتى نتجنب TypeError: Object of type Event is not JSON serializable
user_quiz_runtime: dict[int, dict] = {}

def get_runtime(user_id: int) -> dict:
    return user_quiz_runtime.get(user_id, {})

def set_runtime(user_id: int, **kwargs) -> dict:
    r = user_quiz_runtime.get(user_id) or {}
    r.update(kwargs)
    user_quiz_runtime[user_id] = r
    return r

def clear_runtime(user_id: int):
    user_quiz_runtime.pop(user_id, None)

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
    "timer_tick_seconds": 5,  # تحديث كل 5 ثواني آمن للتلغرام
    "timer_bar_full": "🟩",
    "timer_bar_empty": "⬜",

    # خريطة النقاط لكل نجوم (يمكن أن تُستخدم عند الحاجة)
    "points_per_stars": {"3": 3, "2": 2, "1": 1, "0": 0},

    # تحويل النقاط إلى ليرة: كل points_per_unit نقطة = syp_per_unit ل.س
    "points_conversion_rate": {"points_per_unit": 10, "syp_per_unit": 100},

    # تسعير المحاولة — يدعم الشكلين:
    #   1) {"range":[lo,hi],"price":..}
    #   2) {"min":..,"max":..,"price":..}
    # ضع ما تريد في settings.json وسيُفهم تلقائيًا
    "attempt_price_by_stage": [
        {"min": 1, "max": 1, "price": 25},
        {"min": 2, "max": 2, "price": 50},
        {"min": 3, "max": 4, "price": 75},
        {"min": 5, "max": 6, "price": 100},
        {"min": 7, "max": 8, "price": 125},
        {"min": 9, "max": 10, "price": 150},
        {"min": 11, "max": 12, "price": 175},
        {"min": 13, "max": 14, "price": 200},
        {"min": 15, "max": 16, "price": 225},
        {"min": 17, "max": 999, "price": 250},
    ],

    # سياسة الجوائز: نسبة دفع قصوى من الدخل المتوقع (≈ 2.5 محاولة/سؤال * سعر المحاولة)
    "reward_policy": {
        "target_payout_ratio": 0.30,  # لا ندفع أكثر من 30% كمكافأة مرحلة من R المتوقع
        "bands": [
            {"name": "high", "stars_pct_min": 0.70, "payout_ratio": 1.00},  # 100% من الـ 30% = 30% فعليًا
            {"name": "mid",  "stars_pct_min": 0.50, "payout_ratio": 0.60},  # 60% من 30% = 18%
            {"name": "low",  "stars_pct_min": 0.33, "payout_ratio": 0.25},  # 25% من 30% = 7.5%
        ]
    },
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

def _band_contains(stage_no: int, band: Dict[str, Any]) -> bool:
    if "range" in band and isinstance(band["range"], (list, tuple)) and len(band["range"]) == 2:
        lo, hi = int(band["range"][0]), int(band["range"][1])
        return lo <= stage_no <= hi
    lo = int(band.get("min", 1))
    hi = int(band.get("max", 999))
    return lo <= stage_no <= hi

def get_attempt_price(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    """يدعم شكلين في settings: 'range':[lo,hi] أو 'min'/'max'."""
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
    _ = sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"points": new_pts})
    return (bal, new_pts)

def change_balance(user_id: int, delta: int) -> Tuple[int, int]:
    bal, pts = get_wallet(user_id)
    new_bal = max(0, bal + int(delta))
    _ = sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"balance": new_bal})
    return (new_bal, pts)

def deduct_fee_for_stage(user_id: int, stage_no: int) -> Tuple[bool, int, int]:
    price = get_attempt_price(stage_no)
    bal, _pts = get_wallet(user_id)
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
    _ = sb_update("houssin363", {"user_id": f"eq.{user_id}"}, {"points": 0, "balance": bal + syp})
    return (pts, syp, 0)

# ------------------------ إدارة جلسة اللاعب ------------------------
def get_progress(user_id: int) -> Dict[str, Any]:
    st = user_quiz_state.get(user_id, {})
    return st if st else {}

def reset_progress(user_id: int, template_id: Optional[str] = None) -> Dict[str, Any]:
    t = template_id or pick_template_for_user(user_id)
    state = {
        "template_id": t,
        "stage": 1,
        "q_index": 0,
        "active_msg_id": None,
        "started_at": None,
        # عدّادات المرحلة (نجوم/أخطاء/منجَز) تُصفّر ببداية أي مرحلة
        "stage_stars": 0,
        "stage_wrong_attempts": 0,
        "stage_done": 0,
        # ❗ لا نضع timer_cancel هنا (غير قابل للتسلسل JSON)
    }
    user_quiz_state[user_id] = state
    return state

def _tpl_items_for_stage(tpl: Dict[str, Any], stage_no: int) -> List[Dict[str, Any]]:
    # في القالب: items_by_stage مفاتيحها نصوص
    key = str(stage_no)
    if "items_by_stage" in tpl and key in tpl["items_by_stage"]:
        return tpl["items_by_stage"][key]
    # إن لم توجد، ارجع قائمة فارغة
    return []

def next_question(user_id: int) -> Tuple[Dict[str, Any], Dict[str, Any], int, int]:
    st = get_progress(user_id)
    if not st:
        st = reset_progress(user_id)
    tpl = load_template(st["template_id"])
    stage_no = int(st.get("stage", 1))
    q_idx = int(st.get("q_index", 0))
    arr = _tpl_items_for_stage(tpl, stage_no)

    if q_idx >= len(arr):
        # مرحلة مكتملة → انتقل للمرحلة التالية
        stage_no += 1
        st["stage"] = stage_no
        st["q_index"] = 0
        # صفّر عدّادات المرحلة الجديدة
        st["stage_stars"] = 0
        st["stage_wrong_attempts"] = 0
        st["stage_done"] = 0

        # لو القالب انتهى، نعيده لبداية القالب (أو لاحقًا: بدّل القالب)
        arr = _tpl_items_for_stage(tpl, stage_no)
        if not arr:
            st["stage"] = 1
            stage_no = 1
            st["q_index"] = 0
            arr = _tpl_items_for_stage(tpl, stage_no)

    # ضمان حدود
    if not arr:
        # بدون أسئلة — حالة نادرة: ارجع عنصر وهمي لمنع كسر المنطق
        dummy = {"id": "EMPTY", "text": "لا توجد أسئلة لهذه المرحلة.", "options": ["-"], "correct_index": 0}
        return st, dummy, stage_no, 0

    item = arr[q_idx]
    return st, item, stage_no, q_idx

def advance(user_id: int):
    st = get_progress(user_id)
    st["q_index"] = int(st.get("q_index", 0)) + 1
    user_quiz_state[user_id] = st

# ------------------------ منطق المرحلة: عدد الأسئلة والجائزة ------------------------
def stage_question_count(stage_no: int) -> int:
    """م1–2: 20 سؤال. من م3 فأعلى: 20 + (stage_no-2)*5"""
    return 20 if stage_no <= 2 else 20 + (stage_no - 2) * 5

def _get_stage_counters(user_id: int) -> Tuple[int, int, int]:
    st = user_quiz_state.get(user_id, {})
    stars = int(st.get("stage_stars", 0))
    wrongs = int(st.get("stage_wrong_attempts", 0))
    done = int(st.get("stage_done", 0))
    return stars, wrongs, done

def _reset_stage_counters(user_id: int):
    st = user_quiz_state.get(user_id, {})
    st["stage_stars"] = 0
    st["stage_wrong_attempts"] = 0
    st["stage_done"] = 0
    st["q_index"] = int(st.get("q_index", 0))  # لا نغيّر المؤشر هنا
    user_quiz_state[user_id] = st

def _compute_reward_syp(stars: int, questions: int, stage_no: int, settings: dict) -> int:
    # R المتوقع تقريبًا = 2.5 محاولة/سؤال * سعر المحاولة
    price = get_attempt_price(stage_no, settings)
    expected_R = 2.5 * questions * price

    pol = settings.get("reward_policy", _DEFAULT_SETTINGS["reward_policy"])
    max_payout = float(pol.get("target_payout_ratio", 0.30)) * expected_R
    bands = pol.get("bands", [])

    # نسبة النجوم من الحد الأقصى (3 نجوم/سؤال)
    stars_pct = 0.0 if questions <= 0 else (float(stars) / (3.0 * questions))
    chosen = None
    # اختر أعلى Band محققة للعتبة
    for b in sorted(bands, key=lambda x: float(x.get("stars_pct_min", 0.0)), reverse=True):
        if stars_pct >= float(b.get("stars_pct_min", 0.0)):
            chosen = b
            break
    if not chosen:
        return 0
    payout_ratio = float(chosen.get("payout_ratio", 0.0))
    reward = max_payout * payout_ratio
    return int(round(reward))

def compute_stage_reward_and_finalize(user_id: int, stage_no: int, questions: int) -> dict:
    """
    يحسب جائزة المرحلة بناءً على:
      - عدد النجوم المتراكمة في المرحلة
      - عدد الأسئلة (questions)
      - سياسة الجوائز في settings
    ثم يُودع الجائزة في رصيد المستخدم، ويسجّل ملخصًا في جدول quiz_stage_runs (إن وجد)،
    ويصفّر عدّادات المرحلة.
    """
    settings = load_settings()
    stars, wrongs, done = _get_stage_counters(user_id)

    # لو استدعيت قبل إكمال عدد الأسئلة المتوقع، نستخدم done كمرجع
    total_q = questions if questions > 0 else done
    if done < total_q:
        # مرحلة غير مكتملة — لا مكافأة
        bal, _ = get_wallet(user_id)
        return {
            "questions": done,
            "wrong_attempts": wrongs,
            "stars": stars,
            "reward_syp": 0,
            "balance_after": bal,
        }

    reward = _compute_reward_syp(stars, total_q, stage_no, settings)
    if reward > 0:
        # أودِع الجائزة
        new_bal, _ = change_balance(user_id, reward)
        balance_after = new_bal
    else:
        balance_after, _pts = get_wallet(user_id)

    # سجّل (اختياري) في جدول الملخص — لا يوقف التشغيل لو RLS تمنع
    try:
        payload = {
            "user_id": user_id,
            "stage_no": stage_no,
            "questions": int(total_q),
            "stars": int(stars),
            "wrong_attempts": int(wrongs),
            "reward_syp": int(reward),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _ = sb_upsert("quiz_stage_runs", payload)
    except Exception:
        pass

    # صفّر عدّادات المرحلة (ليبدأ التالية نظيفة)
    _reset_stage_counters(user_id)

    return {
        "questions": int(total_q),
        "wrong_attempts": int(wrongs),
        "stars": int(stars),
        "reward_syp": int(reward),
        "balance_after": int(balance_after),
    }
