
from __future__ import annotations

# Service: Supabase-first storage with local fallback.
# Guarantees:
# - Owner takes a hard 35% net on every attempt (never touched).
# - Player can convert points→balance anytime (1 point = 1 SYP by default).
# - Stage rewards & template-completion awards pay from *op_free_balance* only (no reserve auto-draw).
# - Completion award includes soft-cap and cushion to protect liquidity.

import os, json, math, time, random, hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL","").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY","")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def _sb(table: str, query: str = "") -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}" + (f"?{query}" if query else "")

# ---------- settings ----------
SETTINGS_PATHS = [
    Path(__file__).parent / "content/quiz/settings.json",
    Path(__file__).parent.parent / "content/quiz/settings.json",
    Path("/mnt/data/final_settings.json"),
]
_settings: Dict[str,Any] = {}
def load_settings() -> Dict[str,Any]:
    global _settings
    if _settings: return _settings
    for p in SETTINGS_PATHS:
        if p.exists():
            _settings = json.loads(p.read_text(encoding="utf-8"))
            return _settings
    _settings = {
        "attempts":{"base_price_syp":45,"step_every_stages":2,"step_add_syp":7,"owner_cut_ratio":0.35,"markup_owner_cut_in_price":True},
        "timer":{"stage_time_s":{"1-2":60,"3-5":50,"6+":45}},
        "points":{"manual_conversion_syp_per_point":1.0,"forced_convert_after_stage":2,"value_syp_after_force":1.0},
        "economy":{"winners_ratio":0.65,"reserve_within_winners_ratio":0.30,"progress_pool_ratio":0.0,"min_reserve_draw_threshold":80000,"op_payout_soft_cap_ratio":0.03},
        "completion_award":{"base_award_syp":5000,"soft_cap_ratio_of_op":0.06,"max_award_syp":15000,"cushion_ratio_of_op":0.25,"expected_concurrency":2,"estimated_award_syp":8000},
        "announce":{"enabled":True,"channel_id":-1001234567890,"milestones":[1,5,10],"final_message":"🎉 اللاعب ختم كل الملفات!"},
        "leaderboard":{"top_n":10},
    }
    return _settings

# ---------- helpers ----------
def _qhash(item: Dict[str,Any]) -> str:
    src = json.dumps({"t": item.get("text",""), "opt": item.get("options",[])}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()

# ---------- Wallets / Points ----------
def ensure_user_wallet(user_id: int, name: str):
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            r = cli.get(_sb("houssin363", f"select=*&user_id=eq.{user_id}"), headers=_sb_headers())
            if r.status_code == 200 and r.json(): return
            cli.post(_sb("houssin363"), headers=_sb_headers(), json=[{"user_id": user_id, "name": name}])
    else:
        db = _ld()
        db["wallets"].setdefault(str(user_id), {"user_id": user_id, "name": name, "balance": 0, "points": 0})
        _sd(db)

def get_wallet(user_id: int) -> Dict[str,Any]:
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            r = cli.get(_sb("houssin363", f"select=*&user_id=eq.{user_id}&limit=1"), headers=_sb_headers())
            if r.status_code == 200 and r.json():
                return r.json()[0]
            return {"user_id": user_id, "name": str(user_id), "balance": 0, "points": 0}
    else:
        return _ld()["wallets"].get(str(user_id), {"user_id": user_id, "name": str(user_id), "balance": 0, "points": 0})

def _set_wallet(user_id: int, data: Dict[str,Any]):
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.patch(_sb("houssin363", f"user_id=eq.{user_id}"), headers=_sb_headers(), json=data)
    else:
        db = _ld(); db["wallets"][str(user_id)] = data; _sd(db)

def add_balance(user_id: int, delta: float):
    w = get_wallet(user_id)
    w["balance"] = max(0, float(w.get("balance",0)) + float(delta))
    _set_wallet(user_id, w)

def add_points(user_id: int, pts: int):
    w = get_wallet(user_id)
    w["points"] = max(0, int(w.get("points",0)) + int(pts))
    _set_wallet(user_id, w)

def convert_points_to_balance(user_id: int, all_points: bool = True, pts: Optional[int] = None) -> Tuple[int, float]:
    st = load_settings()
    rate = float(st["points"].get("manual_conversion_syp_per_point", 1.0))
    w = get_wallet(user_id)
    have = int(w.get("points",0))
    if not have: return 0, 0.0
    to_convert = have if all_points else max(0, min(int(pts or 0), have))
    gained = to_convert * rate
    w["points"] = have - to_convert
    w["balance"] = float(w.get("balance",0)) + gained
    _set_wallet(user_id, w)
    _tx({"kind":"points_manual_convert","user_id":user_id,"amount":gained,"points":to_convert})
    return to_convert, gained

# ---------- Progress / Seen ----------
def _first_template_id() -> str:
    p = Path("/mnt/data/final_templates_order.txt")
    if not p.exists(): p = Path(__file__).parent / "final_templates_order.txt"
    if not p.exists(): return "T01"
    ids = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return ids[0] if ids else "T01"

def user_quiz_state(user_id: int) -> Dict[str,Any]:
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            r = cli.get(_sb("quiz_progress", f"select=*&user_id=eq.{user_id}&limit=1"), headers=_sb_headers())
            if r.status_code == 200 and r.json():
                return r.json()[0]
            pr = {"user_id": user_id, "template_id": _first_template_id(), "stage": 1, "q_index": 0, "paid_key": "", "wrong_in_q": 0}
            cli.post(_sb("quiz_progress"), headers=_sb_headers(), json=[pr])
            return pr
    else:
        db = _ld()
        pr = db["progress"].get(str(user_id))
        if not pr:
            pr = {"user_id": user_id, "template_id": _first_template_id(), "stage": 1, "q_index": 0, "paid_key": "", "wrong_in_q": 0}
            db["progress"][str(user_id)] = pr; _sd(db)
        return pr

def reset_progress(user_id: int):
    pr = {"user_id": user_id, "template_id": _first_template_id(), "stage": 1, "q_index": 0, "paid_key": "", "wrong_in_q": 0}
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.post(_sb("quiz_progress"), headers=_sb_headers(), json=[pr])
            cli.delete(_sb("quiz_seen", f"user_id=eq.{user_id}"), headers=_sb_headers())
    else:
        db = _ld(); db["progress"][str(user_id)] = pr; db["seen"][str(user_id)] = set(); _sd(db)

def _update_progress(user_id: int, patch: Dict[str,Any]):
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.patch(_sb("quiz_progress", f"user_id=eq.{user_id}"), headers=_sb_headers(), json=patch)
    else:
        db = _ld(); db["progress"][str(user_id)].update(patch); _sd(db)

def seen_clear_user(user_id: int):
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.delete(_sb("quiz_seen", f"user_id=eq.{user_id}"), headers=_sb_headers())
    else:
        db = _ld(); db["seen"][str(user_id)] = set(); _sd(db)

def _mark_seen(user_id: int, h: str):
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.post(_sb("quiz_seen"), headers=_sb_headers(), json=[{"user_id": user_id, "q_hash": h}])
    else:
        db = _ld(); s = db["seen"].setdefault(str(user_id), set()); s.add(h); db["seen"][str(user_id)] = s; _sd(db)

# ---------- Templates & Questions ----------
def load_template(template_id: str) -> Dict[str,Any]:
    p = Path(__file__).parent / f"{template_id}.json"
    if p.exists(): return json.loads(p.read_text(encoding="utf-8"))
    # fallback generated content (dev only)
    stages = []
    for stg in range(1, 4):
        arr = []
        for i in range(5):
            corr = random.randint(0,3)
            item = {"text": f"[{template_id}] سؤال {stg}-{i+1}", "options": [f"خيار {j+1}" for j in range(4)], "answer": corr}
            item["hash"] = _qhash(item)
            arr.append(item)
        stages.append(arr)
    return {"stages": stages}

def next_question(user_id: int) -> Tuple[Dict[str,Any], int, int]:
    st = user_quiz_state(user_id)
    tpl = load_template(st["template_id"])
    stage_no = st["stage"]
    items = tpl["stages"][stage_no-1]
    # global dedup
    seen = set()
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            resp = cli.get(_sb("quiz_seen", f"select=q_hash&user_id=eq.{user_id}"), headers=_sb_headers())
            if resp.status_code == 200:
                seen = {r["q_hash"] for r in resp.json()}
    else:
        seen = _ld()["seen"].get(str(user_id), set())
    for idx, it in enumerate(items):
        h = it.get("hash") or _qhash(it)
        if h not in seen:
            _update_progress(user_id, {"q_index": idx})
            return it, stage_no, idx
    # stage complete (no more new questions)
    return {}, stage_no, -1

# ---------- Pricing & Timer ----------
def get_attempt_price(stage_no: int) -> int:
    st = load_settings()["attempts"]
    base = int(st.get("base_price_syp",45))
    step_every = int(st.get("step_every_stages",2))
    step_add = int(st.get("step_add_syp",7))
    steps = max(0, (stage_no-1)//max(1,step_every))
    price = base + steps*step_add
    if st.get("markup_owner_cut_in_price", True):
        price = math.ceil(price * (1.0 + float(st.get("owner_cut_ratio",0.35))))
    return int(price)

def get_stage_time(stage_no: int) -> int:
    st = load_settings()["timer"]["stage_time_s"]
    if stage_no <= 2: return int(st.get("1-2",60))
    if stage_no <= 5: return int(st.get("3-5",50))
    return int(st.get("6+",45))

# ---------- Economy ----------
def _eco_get() -> Dict[str,Any]:
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            r = cli.get(_sb("economy_ledger", "select=*&id=eq.global&limit=1"), headers=_sb_headers())
            if r.status_code == 200 and r.json():
                return r.json()[0]
            # ensure row exists
            cli.post(_sb("economy_ledger"), headers=_sb_headers(), json=[{"id":"global"}])
            return {"id":"global","op_free_balance":0.0,"reserve_balance":0.0}
    else:
        return _ld()["economy"]

def _eco_set(e: Dict[str,Any]):
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.patch(_sb("economy_ledger", "id=eq.global"), headers=_sb_headers(), json=e)
    else:
        db = _ld(); db["economy"] = e; _sd(db)

def _tx(row: Dict[str,Any]):
    row = dict(row); row["ts"] = int(time.time())
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.post(_sb("transactions"), headers=_sb_headers(), json=[row])
    else:
        db = _ld(); db["transactions"].append(row); _sd(db)

def ensure_paid_before_show(user_id: int, stage_no: int) -> Tuple[bool, str]:
    price = get_attempt_price(stage_no)
    w = get_wallet(user_id)
    if w.get("balance",0) < price:
        return False, f"⚠️ رصيدك لا يكفي ({int(w.get('balance',0))} ل.س) — السعر {price} ل.س"
    # deduct from wallet
    w["balance"] = float(w.get("balance",0)) - price
    _set_wallet(user_id, w)
    # owner cut 35% hard
    st = load_settings()["attempts"]
    owner_cut = price * float(st.get("owner_cut_ratio",0.35))
    net_after_owner = price - owner_cut
    eco_conf = load_settings()["economy"]
    reserve_within = float(eco_conf.get("reserve_within_winners_ratio",0.30))
    reserved = net_after_owner * reserve_within
    op_add = net_after_owner - reserved
    eco = _eco_get()
    eco["op_free_balance"] = float(eco.get("op_free_balance",0)) + op_add
    eco["reserve_balance"] = float(eco.get("reserve_balance",0)) + reserved
    _eco_set(eco)
    _tx({"kind":"attempt_paid","user_id":user_id,"price":price,"owner_cut":owner_cut,"op_add":op_add,"reserved":reserved})
    # paid_key
    pr = user_quiz_state(user_id)
    pr["paid_key"] = f"{user_id}-{int(time.time()*1000)}"
    _update_progress(user_id, {"paid_key": pr["paid_key"]})
    return True, pr["paid_key"]

def mark_seen_after_payment(user_id: int, item: Dict[str,Any]):
    h = item.get("hash") or _qhash(item)
    _mark_seen(user_id, h)

def register_wrong_attempt(user_id: int):
    st = user_quiz_state(user_id)
    tries = int(st.get("wrong_in_q", 0)) + 1
    _update_progress(user_id, {"wrong_in_q": tries})
    _tx({"kind":"wrong_attempt","user_id":user_id,"tries_in_q":tries})


def compute_stage_reward_syp_safe(stage_no: int) -> float:
    stg_conf = load_settings().get("rewards", {})
    milestones = set(stg_conf.get("milestone_stages", [1,5]))
    if stage_no not in milestones:
        return 0.0
    eco = _eco_get()
    op = float(eco.get("op_free_balance",0.0))
    if op <= 0:
        return 0.0
    per_stage_caps = stg_conf.get("op_soft_cap_ratio_by_stage", {})
    cap_ratio = float(per_stage_caps.get(str(stage_no), load_settings()["economy"].get("op_payout_soft_cap_ratio", 0.03)))
    pay = max(0.0, min(op * cap_ratio, op))
    eco["op_free_balance"] = op - pay
    _eco_set(eco)
    if pay > 0:
        _tx({"kind":"stage_reward","amount":pay,"stage":stage_no})
    return pay

def _stage_count(tpl: Dict[str,Any]) -> int: return len(tpl.get("stages",[]))

def _advance_template(user_id: int):
    order = _templates_order()
    st = user_quiz_state(user_id)
    cur = st["template_id"]
    nxt = order[(order.index(cur)+1) % len(order)] if cur in order else order[0]
    _update_progress(user_id, {"template_id": nxt, "stage": 1, "q_index": 0})

def _templates_order() -> List[str]:
    p = Path("/mnt/data/final_templates_order.txt")
    if not p.exists(): p = Path(__file__).parent / "final_templates_order.txt"
    if p.exists(): return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return ["T01","T02","T03"]

def _maybe_notify_milestone(user_id: int):
    ann = load_settings().get("announce",{})
    if not ann.get("enabled", False): return
    # push outbox row – your bot should read & send
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.post(_sb("notifications_outbox"), headers=_sb_headers(), json=[{"kind":"milestone_hint","payload":{"user_id":user_id}}])
    else:
        _tx({"kind":"announce","user_id":user_id})

def advance_after_correct(user_id: int) -> Tuple[str, Dict[str,Any]]:
    st = user_quiz_state(user_id)
    tpl = load_template(st["template_id"])
    stage_no = st["stage"]
    st_q = st.get("q_index",0) + 1
    items = tpl["stages"][stage_no-1]
    # points based on wrong attempts (3/2/1/0)
    wrong = int(st.get("wrong_in_q", 0))
    gained_pts = 3 if wrong <= 0 else (2 if wrong == 1 else (1 if wrong == 2 else 0))
    if gained_pts > 0:
        add_points(user_id, gained_pts)
        _tx({"kind":"points_gain","user_id":user_id,"points":gained_pts,"stage":stage_no,"q_index":st.get("q_index",0)})
    _update_progress(user_id, {"wrong_in_q": 0})
    if st_q >= len(items):
        # stage done
        reward = compute_stage_reward_syp_safe(stage_no)
        if reward > 0:
            add_balance(user_id, reward)
        extra = 0.0
        if stage_no == int(load_settings().get("rewards",{}).get("top3_after_stage",10)):
            extra = _maybe_top3_award_on_stage10(user_id, st["template_id"])
        _tx({"kind":"stage_done","user_id":user_id,"stage":stage_no,"reward":reward,"extra_top3":extra})
        _update_progress(user_id, {"stage": stage_no+1, "q_index": 0})
        # template done?
        if stage_no + 1 > _stage_count(tpl):
            award = payout_on_template_complete(user_id, st["template_id"])
            _maybe_notify_milestone(user_id)
            _advance_template(user_id)
            return "template_completed", {"award_syp": award}
        else:
            return "stage_completed", {"reward_syp": reward}
    else:
        _update_progress(user_id, {"q_index": st_q})
        return "ok", {"points_gained": gained_pts}

def payout_on_template_complete(user_id: int, template_id: str) -> float:
    conf = load_settings()["completion_award"]
    eco = _eco_get()
    op = float(eco.get("op_free_balance",0.0))
    if op <= 0: return 0.0
    target = min(max(float(conf.get("base_award_syp",0.0)), op * float(conf.get("soft_cap_ratio_of_op",0.06))), float(conf.get("max_award_syp",15000)))
    cushion = op * float(conf.get("cushion_ratio_of_op",0.25)) + int(conf.get("expected_concurrency",2)) * float(conf.get("estimated_award_syp",8000))
    can_pay_now = max(0.0, op - cushion)
    pay = min(target, can_pay_now)
    if pay <= 0:
        _tx({"kind":"template_complete_pending","user_id":user_id,"template_id":template_id})
        return 0.0
    eco["op_free_balance"] = op - pay
    _eco_set(eco)
    add_balance(user_id, pay)
    _tx({"kind":"template_complete_payout","user_id":user_id,"template_id":template_id,"amount":pay})
    # increment completed counter via templates_completed table
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            cli.post(_sb("quiz_templates_completed"), headers=_sb_headers(), json=[{"user_id": user_id, "template_id": template_id}])
    return pay


def _maybe_top3_award_on_stage10(user_id: int, template_id: str) -> float:
    conf = load_settings().get("rewards", {})
    target_stage = int(conf.get("top3_after_stage", 10))
    eco = _eco_get()
    op = float(eco.get("op_free_balance", 0.0))
    if op <= 0:
        return 0.0

    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            chk = cli.get(_sb("quiz_stage_runs", f"select=*,ts&template_id=eq.{template_id}&user_id=eq.{user_id}&stage=eq.{target_stage}&limit=1"), headers=_sb_headers())
            if chk.status_code == 200 and chk.json():
                return 0.0
            cli.post(_sb("quiz_stage_runs"), headers=_sb_headers(), json=[{"user_id": user_id, "template_id": template_id, "stage": target_stage, "ts": int(time.time())}])
            r = cli.get(_sb("quiz_stage_runs", f"select=user_id,ts&template_id=eq.{template_id}&stage=eq.{target_stage}"), headers=_sb_headers())
            if r.status_code != 200: 
                return 0.0
            seen_users = []
            for row in sorted(r.json(), key=lambda x: x.get("ts", 0)):
                uid = row.get("user_id")
                if uid not in seen_users:
                    seen_users.append(uid)
            rank = seen_users.index(user_id) + 1 if user_id in seen_users else 999
    else:
        db = _ld()
        runs = db.setdefault("_local_runs", [])
        if any(rr.get("user_id")==user_id and rr.get("template_id")==template_id and rr.get("stage")==target_stage for rr in runs):
            return 0.0
        runs.append({"user_id":user_id,"template_id":template_id,"stage":target_stage,"ts":int(time.time())})
        _sd(db)
        seen_users = []
        for row in sorted(runs, key=lambda x: x["ts"]):
            if row["template_id"]==template_id and row["stage"]==target_stage:
                if row["user_id"] not in seen_users:
                    seen_users.append(row["user_id"])
        rank = seen_users.index(user_id)+1 if user_id in seen_users else 999

    if rank > 3:
        return 0.0

    ratios = conf.get("top3_awards_ratio_of_op", [0.012, 0.008, 0.006])
    caps = conf.get("top3_awards_max_syp", [25000, 18000, 12000])
    ratio = float(ratios[rank-1]) if rank-1 < len(ratios) else 0.0
    cap = float(caps[rank-1]) if rank-1 < len(caps) else 0.0

    prize = min(op * ratio, cap, op)
    if prize <= 0:
        return 0.0

    eco["op_free_balance"] = float(eco.get("op_free_balance",0.0)) - prize
    _eco_set(eco)
    add_balance(user_id, prize)
    _tx({"kind":"top3_stage10_award","user_id":user_id,"template_id":template_id,"rank":rank,"amount":prize})
    return prize


# ---------- Leaderboard (simple) ----------
def get_leaderboard_top(n: int) -> List[Dict[str,Any]]:
    if USE_SUPABASE:
        with httpx.Client(timeout=10) as cli:
            r = cli.get(_sb("houssin363", f"select=user_id,name,balance&order=balance.desc&limit={max(1,n)}"), headers=_sb_headers())
            if r.status_code == 200: return r.json()
            return []
    else:
        db = _ld()
        rows = [{"user_id": int(uid), "name": w.get("name",""), "balance": w.get("balance",0)} for uid,w in db["wallets"].items()]
        rows.sort(key=lambda r: r["balance"], reverse=True)
        return rows[:max(1,n)]

# ---------- Local fallback store ----------
_LOCAL = Path(__file__).parent / "_local_quiz_db.json"
def _ld() -> Dict[str,Any]:
    if _LOCAL.exists():
        try:
            data = json.loads(_LOCAL.read_text(encoding="utf-8"))
            if isinstance(data.get("seen"), dict):
                for k,v in list(data["seen"].items()):
                    if isinstance(v, list): data["seen"][k] = set(v)
            return data
        except Exception: pass
    data = {"wallets": {}, "progress": {}, "seen": {}, "transactions": [], "economy": {"op_free_balance": 50000.0, "reserve_balance": 0.0}}
    _LOCAL.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8"); return data

def _sd(db: Optional[Dict[str,Any]] = None):
    if db is None: db = _ld()
    if isinstance(db.get("seen"), dict):
        for k,v in list(db["seen"].items()):
            if isinstance(v, set): db["seen"][k] = list(v)
    _LOCAL.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
