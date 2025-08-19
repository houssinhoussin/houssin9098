# services/quiz_service.py
# خدمة مساعدة للعبة: إعدادات، حالة اللاعب، Supabase، عدّادات المرحلة، وحساب جائزة المرحلة كنقاط

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
import time
from datetime import datetime, timedelta

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

# ------------------------ إعداد httpx ثابت ------------------------
def _http_client() -> httpx.Client:
    """
    عميل HTTP ثابت:
    - تعطيل HTTP/2 (بعض المزودين يسببون ReadError تحته).
    - تفعيل retries=3 على مستوى النقل.
    """
    return httpx.Client(
        timeout=20.0,
        http2=False,
        transport=httpx.HTTPTransport(retries=3)
    )

# ------------------------ الإعدادات ------------------------
_DEFAULT_SETTINGS = {
    "seconds_per_question": 50,
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
    # يمكنك ضبط هذه النوافذ/الحدود من settings.json إن رغبت
    "rewards": {
        "window_minutes_t01": 60,
        "window_minutes_t05": 120,
        "t10_daily_top_n": 5,
        # حدود أمان اختيارية لكل صرف (بالليرة) — 0 يعني لا حد
        "t01_max_syp_per_claim": 0,
        "t05_max_syp_per_claim": 0,
        "t10_max_syp_per_claim": 0,
    }
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
    params = {"select": select, "limit": 1}
    params.update(filters)
    with _http_client() as client:
        r = client.get(_table_url(table), headers=_rest_headers(), params=params)
        r.raise_for_status()
        arr = r.json()
        return arr[0] if arr else None

def sb_list(table: str, filters: Dict[str, Any], select: str = "*", limit: int = 10000) -> List[Dict[str, Any]]:
    params = {"select": select, "limit": str(limit)}
    params.update(filters or {})
    with _http_client() as client:
        r = client.get(_table_url(table), headers=_rest_headers(), params=params)
        r.raise_for_status()
        arr = r.json()
        return arr if isinstance(arr, list) else []

def sb_upsert(table: str, row: Dict[str, Any], on_conflict: str | None = None) -> Dict[str, Any]:
    """
    Upsert سليم:
    - يضيف Prefer: resolution=merge-duplicates,return=representation على POST
    - إن رجع 409 (مثلاً السيرفر لم يدمج) نعمل PATCH بالـ filters المبنية من on_conflict
    """
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict

    headers = _rest_headers().copy()
    if on_conflict:
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    else:
        headers["Prefer"] = "return=representation"

    with _http_client() as client:
        r = client.post(_table_url(table), headers=headers, params=params, json=row)
        if r.status_code == 409 and on_conflict:
            # Fallback: PATCH على مفاتيح on_conflict (قد تكون "user_id" أو "col1,col2")
            filters = {}
            keys = [k.strip() for k in on_conflict.split(",") if k.strip()]
            for k in keys:
                v = row.get(k)
                if v is None:
                    continue
                filters[k] = f"eq.{v}"
            r2 = client.patch(_table_url(table), headers=_rest_headers(), params=filters, json=row)
            r2.raise_for_status()
            out2 = r2.json()
            return out2[0] if isinstance(out2, list) and out2 else row

        r.raise_for_status()
        out = r.json()
        return out[0] if isinstance(out, list) and out else row

def sb_update(table: str, filters: Dict[str, Any], patch: Dict[str, Any]) -> List[Dict[str, Any]]:
    params = {}
    params.update(filters)
    with _http_client() as client:
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
        "no_charge_next": int(st.get("no_charge_next", 0)),
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

# === [جديد] تسجيل دخل المحاولات كمعاملة =====================
def _log_attempt_fee(user_id: int, stage_no: int, amount: int):
    try:
        sb_upsert("transactions", {
            "user_id": user_id,
            "kind": "attempt_fee",
            "payload": json.dumps({"stage_no": int(stage_no), "amount": int(amount)}, ensure_ascii=False)
        })
    except Exception:
        pass

def deduct_fee_for_stage(user_id: int, stage_no: int) -> Tuple[bool, int, int]:
    price = get_attempt_price(stage_no)
    bal, _ = get_wallet(user_id)
    if bal < price:
        return (False, bal, price)
    new_bal, _ = change_balance(user_id, -price)
    _log_attempt_fee(user_id, stage_no, price)  # <— تسجيل الدخل
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
            "attempts_on_current": int(row.get("attempts_on_current", 0)),
            "last_click_ts": float(row.get("last_click_ts") or 0.0),
            "paid_key": row.get("paid_key"),
            "no_charge_next": int(row.get("no_charge_next", 0)),
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
        "no_charge_next": 0,
    }
    set_and_persist(user_id, state)
    return state

# ------------------------ أسئلة/مراحل ------------------------
def _timer_bar(remaining: int, settings: Dict[str, Any]) -> str:
    full = settings.get("timer_bar_full", "🟩")
    empty = settings.get("timer_bar_empty", "⬜")
    total = 10
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
      - ok=True و reason in {"already","paid","no-questions","skip-charge"}
      - ok=False و reason="insufficient"
    """
    ensure_user_wallet(user_id)

    st = get_progress(user_id) or reset_progress(user_id)
    tpl_id = st.get("template_id", "T01")
    tpl = load_template(tpl_id)
    stage_no = int(st.get("stage", 1))
    q_idx = int(st.get("q_index", 0))
    items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
    if not items:
        return (True, st.get("last_balance", 0), 0, "no-questions")

    # دعم إعفاء مرة واحدة بعد الإجابة الصحيحة — أو إعفاء مفروض من الهاندلر
    if int(st.get("no_charge_next", 0)) == 1 or get_runtime(user_id).get("force_skip_charge"):
        rt = get_runtime(user_id)
        if "force_skip_charge" in rt:
            rt.pop("force_skip_charge", None)
            _user_runtime[user_id] = rt
        st["no_charge_next"] = 0
        st["paid_key"] = _question_id(tpl_id, stage_no, items[min(q_idx, len(items)-1)], q_idx)
        set_and_persist(user_id, st)
        return (True, st.get("last_balance", 0), get_attempt_price(stage_no), "skip-charge")

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

# --------- [قديم احتياطي] حساب نقاط الجائزة مباشرة (للجولة/الملف) ---------
def _compute_reward_points_direct(template_id: str, stage_no: int, settings: dict) -> int:
    """
    (احتياطي) كان يحسب نقاطًا عند إنهاء الملفات 1/5/10 بناءً على op_free_balance.
    أبقيناه كـ fallback إذا لم يتوفر أي رصيد في أحواض الجوائز الجديدة.
    """
    order = _read_templates_order()
    try:
        t_index = order.index(template_id) + 1  # 1-based
    except ValueError:
        t_index = 0

    tpl = load_template(template_id)
    try:
        last_stage = max(int(k) for k in (tpl.get("items_by_stage") or {}).keys())
    except Exception:
        last_stage = 20
    if stage_no != last_stage:
        return 0

    rewards = settings.get("rewards", {}) or {}
    milestones = rewards.get("round_award_on_templates", [1, 5, 10])
    if t_index not in milestones:
        return 0

    econ = settings.get("economy", {}) or {}
    op_free = int(econ.get("op_free_balance", 0))
    ratio = float(rewards.get("round_award_ratio_of_op", 0.60))
    max_syp_cap = int(rewards.get("round_award_max_syp", 0))  # 0 = لا سقف
    syp_award = int(op_free * max(0.0, ratio))
    if max_syp_cap > 0:
        syp_award = min(syp_award, max_syp_cap)
    if syp_award <= 0:
        return 0
    return _syp_to_points(syp_award, settings)

# ======== [جديد] اقتصاد الجوائز وفق 35%/65% وتقسيم T01/T05/T10/الاحتياطي ========
def _econ_shares() -> Dict[str, float]:
    """
    نسب التوزيع من إجمالي الدخل (attempt_fee):
    - owner: 0.35 (لا يُصرف، فقط للعلم)
    - t01: 0.10
    - t05: 0.20
    - t10: 0.15
    - reserve_points: 0.20  (لتغطية تحويل النقاط إلى رصيد)
    المجموع = 1.00
    """
    return {"owner": 0.35, "t01": 0.10, "t05": 0.20, "t10": 0.15, "reserve_points": 0.20}

def _sum_transactions(kind: str) -> int:
    """
    يجمع المبالغ (amount) من جدول transactions بحسب kind.
    نعتمد تخزين المبلغ داخل payload.amount.
    """
    try:
        rows = sb_list("transactions", {"kind": f"eq.{kind}"}, select="payload", limit=10000)
    except Exception:
        rows = []
    s = 0
    for r in rows:
        try:
            p = r.get("payload")
            if isinstance(p, str):
                p = json.loads(p)
            amt = int(p.get("amount", 0))
            s += max(0, int(amt))
        except Exception:
            continue
    return int(s)

def _sum_convert_spent() -> int:
    """
    مجموع ما صُرف فعليًا في التحويل إلى رصيد (syp_added).
    """
    try:
        rows = sb_list("transactions", {"kind": "eq.convert_points_to_balance"}, select="payload", limit=10000)
    except Exception:
        rows = []
    s = 0
    for r in rows:
        try:
            p = r.get("payload")
            if isinstance(p, str):
                p = json.loads(p)
            s += int(p.get("syp_added", 0))
        except Exception:
            continue
    return int(max(0, s))

def _pools_available() -> Dict[str, int]:
    """
    يحسب الميزانيات المتاحة (بالليرة):
      - t01_avail, t05_avail, t10_avail, reserve_avail

    المتوفر = الحصة الإجمالية (من attempt_fee) - المصروف سابقًا لهذا الحوض.
    """
    shares = _econ_shares()
    total_fee = _sum_transactions("attempt_fee")  # إجمالي الدخل
    total_t01 = int(total_fee * shares["t01"])
    total_t05 = int(total_fee * shares["t05"])
    total_t10 = int(total_fee * shares["t10"])
    total_reserve = int(total_fee * shares["reserve_points"])

    spent_t01 = _sum_transactions("award_t01")
    spent_t05 = _sum_transactions("award_t05")
    spent_t10 = _sum_transactions("award_t10")
    spent_conv = _sum_convert_spent()

    return {
        "t01_avail": max(0, total_t01 - spent_t01),
        "t05_avail": max(0, total_t05 - spent_t05),
        "t10_avail": max(0, total_t10 - spent_t10),
        "reserve_avail": max(0, total_reserve - spent_conv),
    }

def _count_completions_recent(template_id: str, minutes_window: int) -> int:
    """
    يحصي عدد من أكملوا هذا القالب خلال نافذة زمنية حديثة (يعتمد created_at من الجدول quiz_templates_completed).
    إن لم يتوفر الحقل، لن يؤثر؛ سنعيد 0.
    """
    try:
        since = (datetime.utcnow() - timedelta(minutes=max(1, int(minutes_window)))).isoformat()
        rows = sb_list("quiz_templates_completed", {
            "template_id": f"eq.{template_id}",
            "created_at": f"gte.{since}"
        }, select="template_id,created_at", limit=10000)
        return len(rows or [])
    except Exception:
        return 0

def _count_completions_today(template_id: str) -> int:
    try:
        today0 = datetime.utcnow().date().isoformat() + "T00:00:00"
        rows = sb_list("quiz_templates_completed", {
            "template_id": f"eq.{template_id}",
            "created_at": f"gte.{today0}"
        }, select="template_id,created_at", limit=10000)
        return len(rows or [])
    except Exception:
        return 0

def _log_award(kind: str, user_id: int, template_id: str, syp: int, points: int, extra: Dict[str, Any] | None = None):
    payload = {"template_id": template_id, "amount": int(syp), "points": int(points)}
    if extra:
        payload.update(extra)
    try:
        sb_upsert("transactions", {
            "user_id": user_id,
            "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False)
        })
    except Exception:
        pass

def _record_completion(user_id: int, template_id: str, award_points: int, award_syp: int):
    try:
        sb_upsert("quiz_templates_completed", {
            "user_id": user_id,
            "template_id": template_id,
            "payload": json.dumps({"award_points": int(award_points), "award_syp": int(award_syp)}, ensure_ascii=False)
        })
    except Exception:
        pass

def _cap_by(x: int, cap: int) -> int:
    return min(int(x), int(cap)) if int(cap) > 0 else int(x)

# ======== [مهم] صرف الجوائز الديناميكي للحظات إنهاء T01 / T05 / T10 ========
def _compute_dynamic_award_syp(user_id: int, template_id: str, stage_no: int, settings: dict) -> int:
    """
    يقرر كم ل.س تُصرف الآن عند إنهاء ملف معيّن (T01 أو T05 أو T10) وفق الأحواض المتاحة.
    T01: تقسيم لحظي على نافذة زمنية قصيرة.
    T05: مثلها لكن نافذة أطول.
    T10: لأول 5 يوميًا فقط.
    """
    order = _read_templates_order()
    try:
        t_index = order.index(template_id) + 1
    except ValueError:
        t_index = 0

    tpl = load_template(template_id)
    try:
        last_stage = max(int(k) for k in (tpl.get("items_by_stage") or {}).keys())
    except Exception:
        last_stage = 20
    if int(stage_no) != int(last_stage):
        return 0  # ليس عند نهاية الملف

    pools = _pools_available()
    rew = settings.get("rewards", {}) or {}

    if t_index == 1:  # T01
        avail = pools["t01_avail"]
        win_minutes = int(rew.get("window_minutes_t01", 60))
        n_recent = _count_completions_recent(template_id, win_minutes)
        denom = max(1, n_recent + 1)  # نقسّم على عدد الحاليّين + هذا اللاعب
        base = avail // denom
        base = max(0, base)
        cap = int(rew.get("t01_max_syp_per_claim", 0))
        return _cap_by(base, cap)

    if t_index == 5:  # T05
        avail = pools["t05_avail"]
        win_minutes = int(rew.get("window_minutes_t05", 120))
        n_recent = _count_completions_recent(template_id, win_minutes)
        denom = max(1, n_recent + 1)
        base = avail // denom
        base = max(0, base)
        cap = int(rew.get("t05_max_syp_per_claim", 0))
        return _cap_by(base, cap)

    if t_index == 10:  # T10
        avail = pools["t10_avail"]
        top_n = int(rew.get("t10_daily_top_n", 5))
        done_today = _count_completions_today(template_id)
        if done_today >= top_n:
            return 0
        # نضمن عدم تجاوز الحوض: نقسّم ما تبقى على المقاعد المتبقية
        remaining_slots = max(1, top_n - done_today)
        base = avail // remaining_slots
        base = max(0, base)
        cap = int(rew.get("t10_max_syp_per_claim", 0))
        return _cap_by(base, cap)

    return 0

def compute_stage_reward_and_finalize(user_id: int, stage_no: int, questions: int) -> dict:
    """
    يحسب مكافأة المرحلة كنقاط (عند نهاية الملف T01/T05/T10 وفق الأحواض)،
    يضيفها لمحفظة النقاط، ثم يضبط المرحلة التالية ويصفر عدادات المرحلة.
    يرجع: {questions, wrong_attempts, stars, reward_points, points_after}
    """
    settings = load_settings()
    stars, wrongs, done = _get_stage_counters(user_id)
    total_q = questions if questions > 0 else done
    # لو ما خلّص كل أسئلة المرحلة، لا نمنح مكافأة
    if done < total_q:
        _, pts_now = get_wallet(user_id)
        return {"questions": done, "wrong_attempts": wrongs, "stars": stars, "reward_points": 0, "points_after": pts_now}

    # مكافأة الجولة (الملف) عند نهاية الملف — توزيع ديناميكي حسب الأحواض
    st_now = get_progress(user_id) or {}
    tpl_id = st_now.get("template_id", "T01")

    syp_award = _compute_dynamic_award_syp(user_id, tpl_id, stage_no, settings)
    if syp_award <= 0:
        # Fallback قديم (إن رغبتَ) — أو تبقى صفر
        pts_award = _compute_reward_points_direct(tpl_id, stage_no, settings)
        _, pts_after_add = add_points(user_id, int(pts_award))
        # تقدم المرحلة
        st = get_progress(user_id)
        st["stage"] = int(st.get("stage", 1)) + 1
        st["q_index"] = 0
        st.pop("paid_key", None)
        st["no_charge_next"] = 0
        set_and_persist(user_id, st)
        _reset_stage_counters(user_id)
        # سجل إتمام الملف (حتى لو 0)
        _record_completion(user_id, tpl_id, int(pts_award), int(get_points_value_syp(pts_award, settings)))
        return {
            "questions": int(total_q),
            "wrong_attempts": int(wrongs),
            "stars": int(stars),
            "reward_points": int(pts_award),
            "points_after": int(pts_after_add),
        }

    pts_award = _syp_to_points(syp_award, settings)
    _, pts_after_add = add_points(user_id, int(pts_award))
    # سجل الصرف من أي حوض
    order = _read_templates_order()
    try:
        t_index = order.index(tpl_id) + 1
    except ValueError:
        t_index = 0
    kind = "award_t01" if t_index == 1 else ("award_t05" if t_index == 5 else ("award_t10" if t_index == 10 else "award_other"))
    _log_award(kind, user_id, tpl_id, int(syp_award), int(pts_award), extra={"stage_no": int(stage_no)})

    # سجل إتمام الملف (يفيد العدّ اللحظي/اليومي)
    _record_completion(user_id, tpl_id, int(pts_award), int(syp_award))

    # تقدّم المرحلة
    st = get_progress(user_id)
    st["stage"] = int(st.get("stage", 1)) + 1
    st["q_index"] = 0
    st.pop("paid_key", None)        # بداية سؤال جديد ⇒ إزالة مفتاح الدفع
    st["no_charge_next"] = 0        # مع بداية مرحلة جديدة نزيل الإعفاء
    set_and_persist(user_id, st)

    _reset_stage_counters(user_id)

    return {
        "questions": int(total_q),
        "wrong_attempts": int(wrongs),
        "stars": int(stars),
        "reward_points": int(pts_award),
        "points_after": int(pts_after_add),
    }

# ------------------------ عدّادات المرحلة أثناء اللعب ------------------------
def register_wrong_attempt(user_id: int):
    st = get_progress(user_id)
    st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
    st["stage_done"] = int(st.get("stage_done", 0)) + 1
    st["no_charge_next"] = 0  # أي خطأ يلغي الإعفاء
    set_and_persist(user_id, st)

def register_correct_answer(user_id: int):
    st = get_progress(user_id)
    st["stage_stars"] = int(st.get("stage_stars", 0)) + 1
    st["stage_done"] = int(st.get("stage_done", 0)) + 1
    st["no_charge_next"] = 1  # إعفاء خصم لمرة واحدة للسؤال التالي
    set_and_persist(user_id, st)

# ==== [PATCH] إضافات ميزات الجوائز والاقتصاد (بدون تغييرات على الواجهة) ====
def get_stage_time(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
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
    تحويل وحدات نقاط → رصيد حسب points_conversion_rate مع سقف يراعي الاحتياطي (20% من الدخل).
    يرجع (points_before, syp_added, points_after).
    """
    s = load_settings()
    conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    bal, pts = get_wallet(user_id)
    if ppu <= 0 or spu <= 0:
        return pts, 0, pts

    pools = _pools_available()
    reserve_left = int(pools.get("reserve_avail", 0))

    # كم يمكننا تحويله فعليًا الآن حسب الاحتياطي؟
    max_units_by_reserve = reserve_left // max(1, spu)
    units_by_points = pts // ppu
    units = max(0, min(units_by_points, max_units_by_reserve))
    if units <= 0:
        return pts, 0, pts

    pts_spent = units * ppu
    syp_add = units * spu
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

# (الإبقاء على دوال الجوائز الإضافية للمرونة مستقبلاً)
def payout_on_template_complete(user_id: int, template_id: str) -> Dict[str, Any]:
    # لم نعد نستخدمها لصرف رئيسي، لكنها تبقى متاحة للاستخدامات الجانبية/التجريبية.
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
    # أبقيناها كما هي (غير مستخدمة حاليًا)، للمرونة لاحقًا.
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
        try:
            with _http_client() as client:
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
        for i, (uid, _) in enumerate(ranking, start=1):
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
    n = int(max(1, min(100, n)))
    try:
        with _http_client() as client:
            url = _table_url("houssin363")
            headers = _rest_headers()
            params = {"select": "user_id,name,points,balance", "order": "points.desc", "limit": str(n)}
            r = client.get(url, headers=headers, params=params); r.raise_for_status()
            return r.json() or []
    except Exception:
        return []

def get_leaderboard_by_progress(n: int = 10) -> list[dict]:
    n = int(max(1, min(100, n)))
    rows = []
    try:
        with _http_client() as client:
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
            "stage_done": int((r.get("stage_done") or 0)),
        })
    return out

# توافق مع نسخة "الخطأ": دوال لا-أثر
def seen_clear_user(user_id: int):
    return True

def mark_seen_after_payment(user_id: int):
    return True
