# services/quiz_service.py
# خدمة مساعدة للعبة: إعدادات، حالة اللاعب، Supabase، منع التكرار، تسعير/وقت، ولوج المرحلة

from __future__ import annotations
import json
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import httpx

from config import SUPABASE_URL, SUPABASE_KEY
from services.state_adapter import UserStateDictLike  # كاش بالذاكرة فقط

# ------------------------ المسارات ------------------------
BASE = Path("content/quiz")
SETTINGS_PATH = BASE / "settings.json"
ORDER_PATH = BASE / "templates_order.txt"
TEMPLATES_DIR = BASE / "templates"

# ------------------------ كاش الحالة بالذاكرة ------------------------
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

def sb_insert(table: str, row: Dict[str, Any]) -> None:
    with httpx.Client(timeout=20.0) as client:
        client.post(_table_url(table), headers=_rest_headers(), json=row)

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

# ------------------------ قراءة الإعدادات ------------------------
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
        _SETTINGS_CACHE = _safe_json_load(SETTINGS_PATH, {})
    else:
        _SETTINGS_CACHE = {}
    # طبّق قيم افتراضية ضرورية إن غابت
    _SETTINGS_CACHE.setdefault("attempts", {"base_price": 35, "owner_cut_ratio": 0.4})
    _SETTINGS_CACHE.setdefault("ui", {"timer_bar_full": "🟩", "timer_bar_empty": "⬜", "windows_error_template": "", "windows_success_template": ""})
    _SETTINGS_CACHE.setdefault("points_per_stars", {"3": 3, "2": 2, "1": 1, "0": 0})
    _SETTINGS_CACHE.setdefault("timer_tick_seconds", 1)
    return _SETTINGS_CACHE

# ------------------------ زمن السؤال حسب المرحلة ------------------------
def get_seconds_for_stage(stage_no: int, settings: dict | None = None) -> int:
    s = settings or load_settings()
    timers = s.get("timers", {})
    if stage_no <= 2:
        return int(timers.get("stage_1_2", s.get("seconds_per_question", 60)))
    if 3 <= stage_no <= 5:
        return int(timers.get("from_3_to_5", s.get("seconds_per_question", 60)))
    return int(timers.get("from_6_plus", s.get("seconds_per_question", 60)))

# ------------------------ تسعير المحاولة ------------------------
def _band_from_range_obj(band: Dict[str, Any]) -> Tuple[int, int, int]:
    """يدعم صيغتين: {'range':[lo,hi],'price':X} أو {'min':lo,'max':hi,'price':X}"""
    if "range" in band and isinstance(band["range"], list) and len(band["range"]) == 2:
        lo, hi = int(band["range"][0]), int(band["range"][1])
    else:
        lo, hi = int(band.get("min", 1)), int(band.get("max", 999))
    price = int(band.get("price", 0))
    return lo, hi, price

def get_attempt_price(stage_no: int, settings: Dict[str, Any] | None = None) -> int:
    """السعر النهائي الذي يُخصم من اللاعب = السعر الأساسي × (1 + owner_cut_ratio)  ← 25→35، 75→105…"""
    s = settings or load_settings()
    bands = s.get("attempt_price_by_stage") or []
    base = None
    for band in bands:
        lo, hi, price = _band_from_range_obj(band)
        if lo <= stage_no <= hi:
            base = price
            break
    if base is None:
        # Fallback: base_price + step كل عدة مراحل
        att = s.get("attempts", {})
        base = int(att.get("base_price", 35))
        step_every = int(att.get("step_every_stages", 0) or 0)
        step_add   = int(att.get("step_add", 0) or 0)
        if step_every and step_add:
            extra_steps = max(0, (stage_no - 1) // step_every)
            base += extra_steps * step_add
    margin = float(s.get("attempts", {}).get("owner_cut_ratio", 0.4))
    final_price = int(round(base * (1.0 + margin)))
    return final_price

# ------------------------ القوالب ------------------------
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

# ------------------------ تحويل النقاط → رصيد ------------------------
def get_points_value_syp(points: int, settings: Dict[str, Any] | None = None) -> int:
    s = settings or load_settings()
    conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    units = points // ppu
    return units * spu

def convert_points_to_balance(user_id: int) -> Tuple[int, int, int]:
    """
    تحويل يدوي بالنِسب العامة points_conversion_rate.
    يرجع: (pts_before, syp_added, pts_after)
    """
    s = load_settings()
    bal, pts = get_wallet(user_id)
    conv = s.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    if ppu <= 0 or spu <= 0 or pts < ppu:
        return pts, 0, pts
    units = pts // ppu
    syp = units * spu
    pts_left = pts - units * ppu
    change_balance(user_id, syp)
    add_points(user_id, - (units * ppu))
    return pts, syp, pts_left

# ------------------------ خصم آمن قبل عرض السؤال ------------------------
def _current_q_key(user_id: int, tpl_id: str, stage_no: int, q_idx: int, item: Dict[str, Any]) -> str:
    qid = str(item.get("id", q_idx))
    return f"{tpl_id}:{stage_no}:{qid}"

def deduct_fee_for_stage(user_id: int, stage_no: int) -> Tuple[bool, int, int]:
    price = get_attempt_price(stage_no)
    bal, _ = get_wallet(user_id)
    if bal < price:
        return (False, bal, price)
    new_bal, _ = change_balance(user_id, -price)
    # نسجّل عملية مالية بسيطة
    try:
        sb_insert("transactions", {"kind": "quiz_attempt_fee", "amount": price, "meta": {"stage": stage_no}, "ts": int(time.time()*1000), "user_id": user_id})
    except Exception:
        pass
    return (True, new_bal, price)

def ensure_paid_before_show(user_id: int) -> Tuple[bool, int, int, str]:
    """
    يحاول خصم سعر المرحلة مرّة واحدة قبل عرض السؤال الحالي.
    يرجع: (ok, balance_or_new_balance, price, reason)
      - ok=True و reason in {"already","paid","no-questions"}
      - ok=False و reason="insufficient"
    """
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
    # وسم السؤال كمشاهد لمنع تكراره لاحقًا
    try:
        h = q_fingerprint(item)
        seen_add(user_id, h, tpl_id, stage_no)
    except Exception as e:
        print("seen_add mark failed:", e)
    q_key = _current_q_key(user_id, tpl_id, stage_no, q_idx, item)

    # لو مدفوع سابقًا لنفس النسخة فلا نكرر الخصم
    if st.get("paid_key") == q_key:
        bal, _ = get_wallet(user_id)
        return True, bal, 0, "already"

    ok, new_bal_or_old, price = deduct_fee_for_stage(user_id, stage_no)
    if not ok:
        st["last_balance"] = new_bal_or_old
        set_and_persist(user_id, st)
        return False, new_bal_or_old, price, "insufficient"

    st["paid_key"] = q_key
    st["last_balance"] = new_bal_or_old
    set_and_persist(user_id, st)
    return True, new_bal_or_old, price, "paid"

def pause_current_question(user_id: int) -> None:
    """عند الضغط 'أكمل لاحقًا' نزيل وسم الدفع ليُخصم عند الاستئناف."""
    st = get_progress(user_id)
    if st:
        st.pop("paid_key", None)
        set_and_persist(user_id, st)

# ------------------------ جلسة اللاعب ------------------------
def get_progress(user_id: int) -> Dict[str, Any]:
    st = user_quiz_state.get(user_id)
    if st:
        return st
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
    # فلترة الأسئلة التي شوهدت من قبل
    try:
        filtered = []
        for it in arr:
            h = q_fingerprint(it)
            if not seen_exists(user_id, h):
                filtered.append(it)
        if filtered:
            arr = filtered
    except Exception as e:
        print("filter seen failed:", e)

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
    st.pop("paid_key", None)  # سؤال جديد ⇒ خصم جديد
    set_and_persist(user_id, st)

# ------------------------ منطق المرحلة والجوائز (كنقاط) ------------------------
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

def _syp_to_points(amount_syp: int, settings: Dict[str, Any]) -> int:
    conv = settings.get("points_conversion_rate", {"points_per_unit": 10, "syp_per_unit": 5})
    ppu = int(conv.get("points_per_unit", 10))
    spu = int(conv.get("syp_per_unit", 5))
    if spu <= 0:
        return 0
    units = amount_syp // spu
    return units * ppu

def _estimate_stage_reward_syp(stars: int, questions: int, stage_no: int, settings: dict) -> int:
    """تقدير بسيط لجائزة المرحلة (بالليرة) كنسبة من الإيراد المتوقع."""
    price = get_attempt_price(stage_no, settings)
    expected_R = 2.5 * questions * price
    # هدف صرف 30% من الإيراد المتوقع كحد أقصى
    max_payout = 0.30 * expected_R
    # نِطاق اعتمادًا على نسبة النجوم
    stars_pct = 0.0 if questions <= 0 else (float(stars) / (3.0 * questions))
    if stars_pct >= 0.70:
        ratio = 1.00
    elif stars_pct >= 0.50:
        ratio = 0.60
    elif stars_pct >= 0.33:
        ratio = 0.25
    else:
        ratio = 0.0
    return int(round(max_payout * ratio))

def compute_stage_reward_and_finalize(user_id: int, stage_no: int, questions: int) -> dict:
    """
    يحسب مكافأة المرحلة كنقاط، يضيفها لمحفظة النقاط، ثم يضبط المرحلة التالية ويصفر عدادات المرحلة.
    يسجل في quiz_stage_runs: (user_id, template_id, stage, stars, elapsed_ms, ts)
    """
    settings = load_settings()
    stars, wrongs, done = _get_stage_counters(user_id)
    total_q = questions if questions > 0 else done
    if done < total_q:
        _, pts_now = get_wallet(user_id)
        return {"questions": done, "wrong_attempts": wrongs, "stars": stars, "reward_points": 0, "points_after": pts_now}

    reward_syp = _estimate_stage_reward_syp(stars, total_q, stage_no, settings)
    reward_points = _syp_to_points(reward_syp, settings) if reward_syp > 0 else 0
    if reward_points > 0:
        _, pts_after = add_points(user_id, reward_points)
    else:
        _, pts_after = get_wallet(user_id)

    # لوج المتصدرين
    st = get_progress(user_id)
    payload = {
        "user_id": user_id,
        "template_id": st.get("template_id", "T01"),
        "stage": stage_no,
        "stars": int(stars),
        "elapsed_ms": int(max(0, int(time.time()*1000) - int(st.get("started_at") or int(time.time()*1000)))),
        "ts": int(time.time()*1000),
    }
    try:
        sb_upsert("quiz_stage_runs", payload)
    except Exception as e:
        print("quiz_stage_runs insert failed:", e, payload)

    # المرحلة التالية ضمن القالب (أو إعادة للأولى إن انتهت)
    tpl = load_template(st.get("template_id", "T01"))
    next_stage = stage_no + 1
    if str(next_stage) in (tpl.get("items_by_stage", {}) or {}):
        st["stage"] = next_stage
    else:
        st["stage"] = 1
    st["q_index"] = 0
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

# ------------------------ منع تكرار الأسئلة عبر كل الملفات ------------------------
_ARABIC_DIACRITICS = "".join([
    "\u064b","\u064c","\u064d","\u064e","\u064f","\u0650","\u0651","\u0652","\u0670",
    "\u0653","\u0654","\u0655","\u0656","\u0657","\u0658","\u0659","\u065A","\u065B","\u065C","\u065D","\u065E","\u06D6","\u06D7","\u06D8","\u06D9","\u06DA","\u06DB","\u06DC","\u06DF","\u06E0","\u06E1","\u06E2","\u06E3","\u06E4","\u06E5","\u06E6","\u06E7","\u06E8","\u06E9","\u06EA","\u06EB","\u06EC","\u06ED"
])

def _normalize_q_text(s: str) -> str:
    if not s:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFKC", s)
    for ch in _ARABIC_DIACRITICS:
        s = s.replace(ch, "")
    s = s.replace("ـ", "")  # كشيدة
    s = s.replace("أ","ا").replace("إ","ا").replace("آ","ا")
    s = s.replace("ى","ي").replace("ة","ه")
    import re
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s, flags=re.UNICODE).strip().lower()
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    s = s.translate(trans)
    return s

def q_fingerprint(item: Dict[str, Any]) -> str:
    import hashlib, json as _json
    base = item.get("text") or item.get("question") or ""
    norm = _normalize_q_text(base)
    opts = item.get("options") or []
    opts_norm = [_normalize_q_text(o) for o in opts]
    payload = _json.dumps({"t": norm, "o": opts_norm}, ensure_ascii=False, separators=(",",":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

def seen_exists(user_id: int, q_hash: str) -> bool:
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(_table_url("quiz_seen"), headers=_rest_headers(), params={"user_id": f"eq.{user_id}", "q_hash": f"eq.{q_hash}", "select":"q_hash", "limit":"1"})
            if r.status_code == 200 and isinstance(r.json(), list) and r.json():
                return True
    except Exception as e:
        print("seen_exists failed:", e)
    return False

def seen_add(user_id: int, q_hash: str, template_id: str, stage: int):
    try:
        with httpx.Client(timeout=10.0) as client:
            body = {"user_id": int(user_id), "q_hash": q_hash, "template_id": str(template_id), "stage": int(stage)}
            client.post(_table_url("quiz_seen"), headers=_rest_headers(), json=body)
    except Exception as e:
        print("seen_add failed:", e)

def seen_clear_user(user_id: int):
    try:
        with httpx.Client(timeout=10.0) as client:
            client.delete(_table_url("quiz_seen"), headers=_rest_headers(), params={"user_id": f"eq.{user_id}"})
    except Exception as e:
        print("seen_clear_user failed:", e)
