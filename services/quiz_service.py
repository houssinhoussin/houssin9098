# services/quiz_service.py
# خدمة مساعدة للعبة: إعدادات، حالة اللاعب، Supabase، عدّادات المرحلة،
# الجوائز الثابتة (T01/T05/T10)، وإعفاء الخصم عند المتابعة.

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime
import logging

import httpx

from config import SUPABASE_URL, SUPABASE_KEY
from config import SUPABASE_TABLE_NAME
USERS_TABLE = (SUPABASE_TABLE_NAME or 'houssin363')
if USERS_TABLE == 'USERS_TABLE':
    USERS_TABLE = 'houssin363'
from services.state_adapter import UserStateDictLike  # كاش بالذاكرة فقط

# محاولة ربط اختيارية لإبلاغ الإدمن عند قفل المسابقة
try:
    from services.queue_service import add_pending_request as _enqueue_admin
except Exception:
    _enqueue_admin = None# ------------------------ إعداد لوجر بسيط ------------------------
logger = logging.getLogger("quiz_service")

# ------------------------ المسارات ------------------------
BASE = Path("content/quiz")
SETTINGS_PATH = BASE / "settings.json"
ORDER_PATH = BASE / "templates_order.txt"
TEMPLATES_DIR = BASE / "templates"

# ------------------------ كاش الحالة بالذاكرة ------------------------
user_quiz_state = UserStateDictLike()

# حالة منافسة (Fallback بالذاكرة في حال عدم وجود جدول app_state)
_COMP_STATE_FALLBACK = {"cycle": 1, "t10_winners": 0, "locked": False}

# ------------------------ حالة وقتية بالذاكرة ------------------------
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

# ------------------------ httpx ثابت ------------------------
def _http_client() -> httpx.Client:
    return httpx.Client(timeout=20.0, http2=False, transport=httpx.HTTPTransport(retries=3))

# ------------------------ الإعدادات ------------------------
_DEFAULT_SETTINGS = {
    "seconds_per_question": 40,
    "timer_tick_seconds": 5,
    "timer_bar_full": "🟩",
    "timer_bar_empty": "⬜",
    "points_per_stars": {"3": 3, "2": 2, "1": 1, "0": 0},
    "points_conversion_rate": {"points_per_unit": 10, "syp_per_unit": 5},
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
    # جوائز ثابتة كما طُلِب
    "fixed_awards": {
        "t01_syp": 12000,
        "t05_syp": 45000,
        "t10_syp": 500000,
        "t10_top_n": 3
    },
    # fallback في حال عدم وجود "timer.stage_time_s" بالملف
    "timer": {
        "stage_time_s": {
            "1-2": 40,
            "3-5": 35,
            "6+": 30
        }
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
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    headers = _rest_headers().copy()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation" if on_conflict else "return=representation"
    with _http_client() as client:
        r = client.post(_table_url(table), headers=headers, params=params, json=row)
        if r.status_code == 409 and on_conflict:
            # PATCH على مفاتيح on_conflict
            filters = {}
            keys = [k.strip() for k in on_conflict.split(",") if k.strip()]
            for k in keys:
                v = row.get(k)
                if v is not None:
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

def sb_delete(table: str, filters: Dict[str, Any]) -> int:
    """حذف صفوف من جدول بحسب فلاتر PostgREST (eq. / lt. ...)."""
    params = {}
    params.update(filters or {})
    with _http_client() as client:
        r = client.delete(_table_url(table), headers=_rest_headers(), params=params)
        try:
            r.raise_for_status()
        except Exception as e:
            logger.warning("delete from %s failed: %s", table, e)
            return 0
        return 0

# ------------------------ تقدم اللاعب في DB (quiz_progress) ------------------------
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
        logger.warning("quiz_progress upsert failed: %s", e)

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

# ------------------------ زمن كل سؤال حسب المرحلة ------------------------
def get_stage_time(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    """
    يقرأ settings['timer']['stage_time_s'] بنطاقات مثل:
      "1-2": 50,  "3-5": 45,  "6+": 40
    وإلا يرجع seconds_per_question الافتراضي.
    """
    s = settings or load_settings()
    default_s = int(s.get("seconds_per_question", _DEFAULT_SETTINGS["seconds_per_question"]))
    timer_cfg = (s.get("timer") or {}).get("stage_time_s") or {}
    try:
        stage_no = int(stage_no)
    except Exception:
        stage_no = 1
    for rng, val in timer_cfg.items():
        try:
            if rng.endswith("+"):
                lo = int(rng[:-1])
                if stage_no >= lo:
                    return int(val)
            elif "-" in rng:
                lo, hi = [int(x) for x in rng.split("-", 1)]
                if lo <= stage_no <= hi:
                    return int(val)
        except Exception:
            continue
    return default_s

# ------------------------ ترتيب القوالب ------------------------
def _read_templates_order() -> List[str]:
    if not ORDER_PATH.exists():
        return []
    arr = [x.strip() for x in ORDER_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [x for x in arr if x]

def _tpl_index(template_id: str) -> int:
    order = _read_templates_order()
    try:
        return order.index(template_id) + 1
    except ValueError:
        return 0

# ------------------------ محاسبة/اقتصاد ------------------------
def _band_contains(stage_no: int, band: Dict[str, Any]) -> bool:
    lo = int(band.get("min", 1)); hi = int(band.get("max", 999))
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
    ppu = int(conv.get("points_per_unit", 10)); spu = int(conv.get("syp_per_unit", 5))
    if ppu <= 0 or spu <= 0:
        return 0
    return (int(points) * spu) // ppu

def _syp_to_points(syp: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    conv = s.get("points_conversion_rate", _DEFAULT_SETTINGS["points_conversion_rate"])
    ppu = int(conv.get("points_per_unit", 10)); spu = int(conv.get("syp_per_unit", 5))
    if ppu <= 0 or spu <= 0:
        return 0
    return (int(syp) * ppu) // spu

# ------------------------ القوالب ------------------------
def load_template(requested_template_id: str, refresh: bool = False) -> Dict[str, Any]:
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

# ------------------------ محفظة/نقاط (USERS_TABLE) ------------------------
def ensure_user_wallet(user_id: int, name: str | None = None) -> Dict[str, Any]:
    row = sb_select_one(USERS_TABLE, {"user_id": f"eq.{user_id}"})
    if row:
        return row
    return sb_upsert("USERS_TABLE", {"user_id": user_id, "name": name or "", "balance": 0, "points": 0}, on_conflict="user_id")

def get_wallet(user_id: int) -> Tuple[int, int]:
    row = sb_select_one(USERS_TABLE, {"user_id": f"eq.{user_id}"}, select="balance,points")
    if not row:
        return (0, 0)
    return int(row.get("balance") or 0), int(row.get("points") or 0)

def add_points(user_id: int, delta: int) -> Tuple[int, int]:
    bal, pts = get_wallet(user_id)
    new_pts = max(0, pts + int(delta))
    sb_update(USERS_TABLE, {"user_id": f"eq.{user_id}"}, {"points": new_pts})
    return (bal, new_pts)

def change_balance(user_id: int, delta: int) -> Tuple[int, int]:
    bal, pts = get_wallet(user_id)
    new_bal = max(0, bal + int(delta))
    sb_update(USERS_TABLE, {"user_id": f"eq.{user_id}"}, {"balance": new_bal})
    return (new_bal, pts)

# تسجيل دخل المحاولة
def _log_attempt_fee(user_id: int, stage_no: int, amount: int):
    try:
        sb_upsert("transactions", {
            "user_id": user_id,
            "kind": "attempt_fee",
            "payload": {"stage_no": int(stage_no), "amount": int(amount)}
        })
    except Exception as e:
        logger.warning("log attempt_fee failed: %s", e)

def deduct_fee_for_stage(user_id: int, stage_no: int) -> Tuple[bool, int, int]:
    price = get_attempt_price(stage_no)
    bal, _ = get_wallet(user_id)
    if bal < price:
        return (False, bal, price)
    new_bal, _ = change_balance(user_id, -price)
    _log_attempt_fee(user_id, stage_no, price)
    return (True, new_bal, price)

# ------------------------ التقدم (ذاكرة) ------------------------
def get_progress(user_id: int) -> Dict[str, Any]:
    st = user_quiz_state.get(user_id)
    if st:
        return st
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
def _question_id(tpl_id: str, stage_no: int, item: dict, q_idx: int) -> str:
    qid = str(item.get("id", q_idx))
    return f"{tpl_id}:{stage_no}:{qid}"

def ensure_paid_before_show(user_id: int) -> Tuple[bool, int, int, str]:
    """
    يحاول خصم سعر المرحلة مرّة واحدة قبل عرض السؤال الحالي.
    إعفاء الخصم:
      - بعد إجابة صحيحة مباشرة (no_charge_next=1)
      - أو عند استدعاء set_runtime(user_id, force_skip_charge=True) من الهاندلر (زر المتابعة)
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

    # إعفاء بعد الإجابة الصحيحة أو إعفاء مفروض من الهاندلر (زر المتابعة)
    if int(st.get("no_charge_next", 0)) == 1 or get_runtime(user_id).get("force_skip_charge"):
        rt = get_runtime(user_id)
        if "force_skip_charge" in rt:
            rt.pop("force_skip_charge", None); _user_runtime[user_id] = rt
        st["no_charge_next"] = 0
        st["paid_key"] = _question_id(tpl_id, stage_no, items[min(q_idx, len(items)-1)], q_idx)
        set_and_persist(user_id, st)
        return (True, st.get("last_balance", 0), get_attempt_price(stage_no), "skip-charge")

    # خصم مرة واحدة لكل سؤال
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
        q_idx = len(arr) - 1
    item = arr[q_idx]
    return st, item, stage_no, q_idx

def advance(user_id: int):
    st = get_progress(user_id)
    st["q_index"] = int(st.get("q_index", 0)) + 1
    st.pop("paid_key", None)
    set_and_persist(user_id, st)

# ------------------------ عدّادات المرحلة أثناء اللعب ------------------------
def stage_question_count(stage_no: int) -> int:
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

def register_wrong_attempt(user_id: int):
    """تُسجّل محاولة خاطئة (لا ترفع السؤال الحالي)."""
    try:
        st = get_progress(user_id) or reset_progress(user_id)
        st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
        # نعدّ عدد المحاولات لهذا السؤال لنحسب نقاط أول صح لاحقًا
        st["attempts_on_current"] = int(st.get("attempts_on_current", 0)) + 1
        st["no_charge_next"] = 0  # لا إعفاء بعد خطأ
        set_and_persist(user_id, st)
    except Exception as e:
        logger.warning("register_wrong_attempt failed: %s", e)

def register_correct_answer(user_id: int):
    """تُسجّل إجابة صحيحة وتُفعّل إعفاء الخصم للسؤال التالي."""
    try:
        st = get_progress(user_id) or reset_progress(user_id)
        st["stage_stars"] = int(st.get("stage_stars", 0)) + 1
        st["stage_done"] = int(st.get("stage_done", 0)) + 1
        st["no_charge_next"] = 1  # إعفاء للسؤال التالي (ما لم يضغط أكمل لاحقًا)
        set_and_persist(user_id, st)
    except Exception as e:
        logger.warning("register_correct_answer failed: %s", e)

def award_points_for_correct(user_id: int, template_id: str, stage_no: int, item: dict, q_idx: int) -> Tuple[int, int, int]:
    """
    يمنح نقاطًا فورية عند أول إجابة صحيحة لسؤال معيّن بحسب ترتيب المحاولة:
      1st: +3، 2nd: +2، 3rd: +1، 4+: 0
    يُسجّل في transactions(kind='points_award', payload={delta, stage_no, qid})
    يرجع (delta_points, new_points, balance)
    """
    st = get_progress(user_id) or reset_progress(user_id)
    wrong_before = int(st.get("attempts_on_current", 0))  # عدد محاولات هذا السؤال قبل أول صح
    if wrong_before <= 0:
        delta = 3
    elif wrong_before == 1:
        delta = 2
    elif wrong_before == 2:
        delta = 1
    else:
        delta = 0

    # أضف النقاط فورًا
    _, new_pts = add_points(user_id, delta)
    bal, pts = get_wallet(user_id)

    # سجّل العملية
    try:
        sb_upsert("transactions", {
            "user_id": user_id,
            "kind": "points_award",
            "payload": {
                "delta": int(delta),
                "stage_no": int(stage_no),
                "qid": _question_id(template_id, stage_no, item, q_idx)
            }
        })
    except Exception as e:
        logger.warning("log points_award failed: %s", e)

    return int(delta), int(pts), int(bal)

# ------------------------ إدارة دورة المسابقة (T10) ------------------------
def _comp_state_get() -> Dict[str, Any]:
    # نحاول من جدول app_state(key,value json) وإلا Fallback بالذاكرة
    try:
        row = sb_select_one("app_state", {"key": "eq.quiz_competition_state"}, select="key,value")
        if row and row.get("value") is not None:
            v = row["value"]
            if isinstance(v, str):
                v = json.loads(v)
            if not isinstance(v, dict):
                v = {}
            for k, d in {"cycle": 1, "t10_winners": 0, "locked": False}.items():
                v.setdefault(k, d)
            return v
    except Exception as e:
        logger.warning("comp_state_get failed: %s", e)
    return dict(_COMP_STATE_FALLBACK)

def _comp_state_set(state: Dict[str, Any]):
    try:
        sb_upsert("app_state", {"key": "quiz_competition_state", "value": state}, on_conflict="key")
    except Exception as e:
        logger.warning("comp_state_set failed: %s", e)
        _COMP_STATE_FALLBACK.update(state)

def admin_reset_competition():
    st = _comp_state_get()
    st["cycle"] = int(st.get("cycle", 1)) + 1
    st["t10_winners"] = 0
    st["locked"] = False
    _comp_state_set(st)
    return st

def _notify_admin_restart(payload: Dict[str, Any]):
    # أرسل طلب موافقة/تنبيه للإدمن
    try:
        if callable(_enqueue_admin):
            _enqueue_admin(
                user_id=0,
                action="competition_restart",
                payload=payload,
                approve_channel="admin",
                meta={"kind": "quiz_competition", "ts": datetime.utcnow().isoformat()}
            )
        else:
            sb_upsert("transactions", {
                "user_id": 0,
                "kind": "admin_notify",
                "payload": {"action": "competition_restart", **payload}
            })
    except Exception as e:
        logger.warning("notify_admin failed: %s", e)

# ------------------------ صرف الجوائز الثابتة ------------------------
def _log_award(kind: str, user_id: int, template_id: str, syp: int, points: int, extra: Dict[str, Any] | None = None):
    payload = {"template_id": template_id, "amount": int(syp), "points": int(points)}
    if extra:
        payload.update(extra)
    try:
        sb_upsert("transactions", {
            "user_id": user_id,
            "kind": kind,
            "payload": payload
        })
    except Exception as e:
        logger.warning("log award failed: %s", e)

def _record_completion(user_id: int, template_id: str, award_points: int, award_syp: int):
    """
    يسجّل الإتمام لأول مرة فقط لكل template_id لكل لاعب (منع التكرار).
    """
    try:
        sb_upsert("quiz_templates_completed", {
            "user_id": user_id,
            "template_id": template_id,
            "payload": {"award_points": int(award_points), "award_syp": int(award_syp)}
        }, on_conflict="user_id,template_id")
    except Exception as e:
        logger.warning("record completion failed: %s", e)

def _last_stage_of_template(template_id: str) -> int:
    tpl = load_template(template_id)
    try:
        return max(int(k) for k in (tpl.get("items_by_stage") or {}).keys())
    except Exception:
        return 20

def _user_completed_set(user_id: int) -> set[str]:
    """
    يرجّع مجموعة القوالب التي أكملها اللاعب سابقًا (Distinct).
    """
    try:
        rows = sb_list("quiz_templates_completed", {"user_id": f"eq.{user_id}"}, select="template_id", limit=10000)
        return {str(r.get("template_id")) for r in rows if r.get("template_id")}
    except Exception:
        return set()

def _award_fixed_syp_for_completion_index(n_completed_after: int, settings: Dict[str, Any] | None = None) -> Tuple[int, str]:
    """
    يمنح جوائز ثابتة حسب ترتيب الإكمال الإجمالي للاعب:
      1st completion  -> t01_syp
      5th completion  -> t05_syp
      10th completion -> t10_syp
    """
    s = settings or load_settings()
    f = (s.get("fixed_awards") or {})
    if n_completed_after == 1:
        return int(f.get("t01_syp", 12000)), "award_t01"
    if n_completed_after == 5:
        return int(f.get("t05_syp", 45000)), "award_t05"
    if n_completed_after == 10:
        return int(f.get("t10_syp", 500000)), "award_t10"
    return 0, "award_other"

def compute_stage_reward_and_finalize(user_id: int, stage_no: int, questions: int) -> dict:
    """
    عند نهاية القالب (Template):
      - أول إكمال في تاريخ اللاعب: 12,000 ل.س ثابتة
      - خامس إكمال: 45,000 ل.س ثابتة
      - عاشر إكمال: 500,000 ل.س لأول 3 فقط، ثم يُقفل السباق ويُبلغ الإدمن
    تُصرف كـ نقاط (وفق التحويل) وتُسجّل العمليات وتُقدَّم المرحلة.
    """
    settings = load_settings()
    stars, wrongs, done = _get_stage_counters(user_id)
    total_q = questions if questions > 0 else done
    if done < total_q:
        _, pts_now = get_wallet(user_id)
        return {"questions": done, "wrong_attempts": wrongs, "stars": stars, "reward_points": 0, "points_after": pts_now}

    st_now = get_progress(user_id) or {}
    tpl_id = st_now.get("template_id", "T01")
    # لا صرف إلا عند نهاية الملف
    if int(stage_no) != _last_stage_of_template(tpl_id):
        _, pts_now = get_wallet(user_id)
        st = get_progress(user_id); st["stage"] = int(st.get("stage", 1)) + 1; st["q_index"] = 0
        st.pop("paid_key", None)
        # مهم: إجابة صحيحة أنهت المرحلة ⇒ إعفاء لسؤال البداية القادم إذا انتقل فورًا
        st["no_charge_next"] = 1
        set_and_persist(user_id, st)
        _reset_stage_counters(user_id)
        return {"questions": int(total_q), "wrong_attempts": int(wrongs), "stars": int(stars), "reward_points": 0, "points_after": int(pts_now)}

    # --- منطق الجوائز الجديد حسب عدد القوالب المكتملة ---
    completed_before = _user_completed_set(user_id)
    already_done_this_template = (tpl_id in completed_before)

    syp_award = 0
    kind = "award_other"

    if not already_done_this_template:
        n_after = len(completed_before) + 1
        syp_award, kind = _award_fixed_syp_for_completion_index(n_after, settings)

        # جائزة الإكمال العاشر: طبّق حدّ top_n وإقفال المسابقة كما في السابق
        if kind == "award_t10":
            comp = _comp_state_get()
            top_n = int((settings.get("fixed_awards") or {}).get("t10_top_n", 3))
            if comp.get("locked"):
                syp_award = 0  # مقفلة
            elif int(comp.get("t10_winners", 0)) >= top_n:
                comp["locked"] = True
                _comp_state_set(comp)
                syp_award = 0
            else:
                comp["t10_winners"] = int(comp.get("t10_winners", 0)) + 1
                if comp["t10_winners"] >= top_n:
                    comp["locked"] = True
                    _notify_admin_restart({"reason": "t10_winners_reached", "cycle": int(comp.get("cycle", 1)), "winners": comp["t10_winners"]})
                _comp_state_set(comp)

    # صرف كنقاط (وفق معدّل التحويل) إن وُجدت جائزة
    pts_award = _syp_to_points(int(max(0, syp_award)), settings) if syp_award > 0 else 0
    if pts_award > 0:
        _, pts_after_add = add_points(user_id, int(pts_award))
    else:
        _, pts_after_add = get_wallet(user_id)

    # سجل المعاملة والإتمام
    _log_award(kind, user_id, tpl_id, int(syp_award), int(pts_award), extra={"stage_no": int(stage_no)})

    if not already_done_this_template:
        _record_completion(user_id, tpl_id, int(pts_award), int(syp_award))

    # تقدّم المرحلة
    st = get_progress(user_id)
    st["stage"] = int(st.get("stage", 1)) + 1
    st["q_index"] = 0
    st.pop("paid_key", None)
    # مهم: انتهت المرحلة بإجابة صحيحة ⇒ إعفاء للسؤال الأول في المرحلة التالية
    st["no_charge_next"] = 1
    set_and_persist(user_id, st)

    _reset_stage_counters(user_id)

    return {
        "questions": int(total_q),
        "wrong_attempts": int(wrongs),
        "stars": int(stars),
        "reward_points": int(pts_award),
        "points_after": int(pts_after_add),
    }

# ------------------------ عرض الرصيد والنقاط للمستخدم ------------------------
def get_wallet_view(user_id: int) -> Dict[str, int]:
    """
    يعيد:
      - balance: رصيد الليرة
      - points: رصيد النقاط
      - points_value_syp: قيمة النقاط التقريبية بالليرة (للإظهار فقط)
      - convertible_now_syp: الحد الأقصى الممكن تحويله الآن بالليرة (إن لم يكن لديك قيد احتياطي خارجي)
    """
    bal, pts = get_wallet(user_id)
    s = load_settings()
    conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = max(1, int(conv.get("points_per_unit", 10)))
    spu = max(1, int(conv.get("syp_per_unit", 5)))
    points_value_syp = (int(pts) * spu) // ppu
    convertible_now_syp = points_value_syp
    return {
        "balance": int(bal),
        "points": int(pts),
        "points_value_syp": int(points_value_syp),
        "convertible_now_syp": int(convertible_now_syp),
    }

# (اختياري) تحويل النقاط إلى رصيد — يستهلك كل ما أمكن وفق الرصيد النظري
def convert_points_to_balance(user_id: int):
    s = load_settings()
    conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = int(conv.get("points_per_unit", 10)); spu = int(conv.get("syp_per_unit", 5))
    bal, pts = get_wallet(user_id)
    if ppu <= 0 or spu <= 0:
        return pts, 0, pts
    units = pts // ppu
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
            "payload": {"units": units, "points_spent": pts_spent, "syp_added": syp_add}
        })
    except Exception as e:
        logger.warning("log convert_points_to_balance failed: %s", e)
    return pts, syp_add, pts_after

# لوائح وشاشات ترتيب
def get_leaderboard_top(n: int = 10) -> list[dict]:
    n = int(max(1, min(100, n)))
    try:
        with _http_client() as client:
            url = _table_url(USERS_TABLE)
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
            wallet = sb_select_one(USERS_TABLE, {"user_id": f"eq.{uid}"}, select="name,points,balance")
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

# أدوات بدء جديد
def wipe_user_for_fresh_start(user_id: int):
    """
    يصفر نقاط اللاعب ويحذف تقدّمه (لا يمس رصيده النقدي).
    """
    try:
        sb_update(USERS_TABLE, {"user_id": f"eq.{user_id}"}, {"points": 0})
        sb_delete("quiz_progress", {"user_id": f"eq.{user_id}"})
        sb_delete("quiz_templates_completed", {"user_id": f"eq.{user_id}"})
    except Exception as e:
        logger.warning("wipe_user_for_fresh_start failed: %s", e)
    user_quiz_state.pop(user_id, None)
    reset_progress(user_id)

# توافق
def seen_clear_user(user_id: int): return True
def mark_seen_after_payment(user_id: int): return True
