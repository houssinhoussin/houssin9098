# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta, timezone
import logging

from database.db import get_table

# ساعة موحّدة (UTC)
try:
    from utils.time import now as _now  # يفترض أنها ترجع datetime مع timezone.utc
except Exception:
    def _now() -> datetime:
        return datetime.now(timezone.utc)

DISCOUNTS_TABLE = "discounts"
USES_TABLE      = "discount_uses"


# --------- أدوات وقتية ---------

def _parse_dt(val) -> Optional[datetime]:
    if not val:
        return None
    try:
        if isinstance(val, datetime):
            return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        if isinstance(val, str):
            s = val[:-1] + "+00:00" if val.endswith("Z") else val
            return datetime.fromisoformat(s)
    except Exception:
        return None
    return None

def _time_window_filter(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    فلترة زمنية: starts_at <= الآن و (ends_at is null أو ends_at > الآن)
    """
    now = _now()
    ok: List[Dict[str, Any]] = []
    for r in rows:
        st = _parse_dt(r.get("starts_at")) or now
        en = _parse_dt(r.get("ends_at"))
        if st <= now and (en is None or en > now):
            ok.append(r)
    return ok


# --------- عمليات أساسية على الخصومات ---------

def list_discounts(limit: int = 100) -> List[Dict[str, Any]]:
    """
    عرض آخر الخصومات مع حقل computed: effective_active = active and not expired (حتى الآن).
    """
    try:
        res = (
            get_table(DISCOUNTS_TABLE)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = getattr(res, "data", []) or []
        now = _now()
        for r in rows:
            ends = _parse_dt(r.get("ends_at"))
            ended = (ends is not None) and (ends <= now)
            r["effective_active"] = bool(r.get("active")) and not ended
        return rows
    except Exception as e:
        logging.exception("[discounts] list failed: %s", e)
        return []


def create_discount(scope: str,
                    percent: int,
                    user_id: int | None = None,
                    active: bool = True,
                    days: int | None = None,
                    hours: int | None = None,
                    source: str | None = None,
                    meta: dict | None = None):
    """
    إنشاء خصم جديد. لو مرّرت hours/days سيتحدد ends_at تلقائيًا.
    scope: 'global' | 'user' | 'product' | 'code'
    source: مثال 'admin' أو 'referral'
    """
    scope = (scope or "global").lower()
    percent = max(0, min(int(percent or 0), 100))

    row: Dict[str, Any] = {
        "scope": scope,
        "percent": percent,
        "active": bool(active),
        "starts_at": _now().isoformat(),
    }
    if scope == "user" and user_id:
        row["user_id"] = int(user_id)

    delta = None
    if hours and int(hours) > 0:
        delta = timedelta(hours=int(hours))
    elif days and int(days) > 0:
        delta = timedelta(days=int(days))
    if delta:
        row["ends_at"] = (_now() + delta).isoformat()

    if source:
        row["source"] = source
    if meta:
        row["meta"] = meta

    res = get_table(DISCOUNTS_TABLE).insert(row).execute()
    return res.data[0] if hasattr(res, "data") and res.data else None


def end_discount_now(did: str) -> bool:
    """تعطيل فوري وتعيين ends_at = الآن."""
    try:
        get_table(DISCOUNTS_TABLE).update(
            {"active": False, "ends_at": _now().isoformat()}
        ).eq("id", did).execute()
        return True
    except Exception as e:
        logging.exception("[discounts] end now failed: %s", e)
        return False


def delete_discount(did: str) -> bool:
    try:
        get_table(DISCOUNTS_TABLE).delete().eq("id", did).execute()
        return True
    except Exception as e:
        logging.exception("[discounts] delete failed: %s", e)
        return False


def set_discount_active(did: str, active: bool) -> bool:
    try:
        get_table(DISCOUNTS_TABLE).update({"active": bool(active)}).eq("id", did).execute()
        return True
    except Exception as e:
        logging.exception("[discounts] toggle failed: %s", e)
        return False


# --------- اختيار أعلى خصم فعّال (قديم) ---------

def get_active_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    """
    يعيد أعلى خصم فعّال (global أو user) للمستخدم، بدون جمع.
    يبقى موجودًا للتوافق الخلفي.
    """
    try:
        res = get_table(DISCOUNTS_TABLE).select("*").eq("active", True).execute()
        rows: List[Dict[str, Any]] = getattr(res, "data", []) or []
    except Exception as e:
        logging.exception("[discounts] get_active_for_user failed: %s", e)
        return None

    rows = _time_window_filter(rows)

    best: Optional[Dict[str, Any]] = None
    for r in rows:
        sc = (r.get("scope") or "global").lower()
        ok = (sc == "global") or (sc == "user" and int(r.get("user_id") or 0) == int(user_id))
        if not ok:
            continue
        if (best is None) or (int(r.get("percent") or 0) > int(best.get("percent") or 0)):
            best = r
    return best


# --------- جمع خصم الأدمن + خصم الإحالة ---------

def _list_active_for_user(user_id: int) -> List[Dict[str, Any]]:
    try:
        res = get_table(DISCOUNTS_TABLE).select("*").eq("active", True).execute()
        rows = getattr(res, "data", []) or []
    except Exception:
        rows = []
    rows = _time_window_filter(rows)

    out = []
    for r in rows:
        sc = (r.get("scope") or "global").lower()
        if sc == "global":
            out.append(r)
        elif sc == "user" and int(r.get("user_id") or 0) == int(user_id):
            out.append(r)
        # يمكن لاحقًا دعم product/code إذا رغبت
    return out


def apply_discount_stacked(user_id: int, amount_syp: int) -> Tuple[int, Dict[str, Any]]:
    """
    يجمع أعلى خصم من نوع 'admin' (أو NULL) + أعلى خصم من نوع 'referral'.
    السقف 100%. يرجع (السعر بعد الخصم, info={percent,breakdown})
    """
    rows = _list_active_for_user(user_id)

    admin_pct = 0  # أي خصم مصدره admin أو NULL
    referral_pct = 0

    for r in rows:
        src = (r.get("source") or "admin").lower()
        pct = int(r.get("percent") or 0)
        if src == "referral":
            referral_pct = max(referral_pct, pct)
        else:
            admin_pct = max(admin_pct, pct)

    total_pct = min(100, admin_pct + referral_pct)
    after = int(round(int(amount_syp) * (100 - total_pct) / 100.0))
    info = {"percent": total_pct, "breakdown": []}
    if admin_pct:
        info["breakdown"].append({"source": "admin", "percent": admin_pct})
    if referral_pct:
        info["breakdown"].append({"source": "referral", "percent": referral_pct})

    return after, info


# --------- تسجيل استخدام الخصم ---------

def record_discount_use(discount_id: str, user_id: int, amount_before: int, amount_after: int, purchase_id: Optional[int] = None) -> None:
    try:
        get_table(USES_TABLE).insert({
            "discount_id": discount_id,
            "user_id": user_id,
            "amount_before": int(amount_before),
            "amount_after":  int(amount_after),
            "purchase_id": purchase_id
        }).execute()
    except Exception as e:
        logging.exception("[discounts] record use failed: %s", e)


# --------- الدالة المطلوبة من admin.py: discount_stats ---------

def discount_stats(days: int = 30) -> List[str]:
    """
    تُعيد ملخصًا نصّيًا بسيطًا لإحصائيات الخصومات آخر (days) يومًا.
    الناتج: List[str] لتلائُم استدعاء لوحة الأدمن.
    """
    try:
        res = get_table(DISCOUNTS_TABLE).select(
            "id, percent, active, source, starts_at, ends_at, created_at"
        ).order("created_at", desc=True).limit(1000).execute()
        rows = getattr(res, "data", []) or []
    except Exception as e:
        logging.exception("[discounts] stats failed: %s", e)
        rows = []

    now = _now()
    act, exp, admin_n, ref_n = 0, 0, 0, 0
    top_admin, top_ref = 0, 0
    nearest_end = None

    for r in rows:
        pct = int(r.get("percent") or 0)
        src = (r.get("source") or "admin").lower()
        st  = _parse_dt(r.get("starts_at")) or now
        en  = _parse_dt(r.get("ends_at"))

        is_active_time = st <= now and (en is None or en > now)
        is_active_flag = bool(r.get("active"))

        if is_active_time and is_active_flag:
            act += 1
        else:
            exp += 1

        if src == "referral":
            ref_n += 1
            top_ref = max(top_ref, pct)
        else:
            admin_n += 1
            top_admin = max(top_admin, pct)

        if en and en > now:
            if nearest_end is None or en < nearest_end:
                nearest_end = en

    out = [
        f"النشِطة حاليًا: {act}",
        f"منتهية/غير نشِطة: {exp}",
        f"خصومات الأدمن: {admin_n} (أعلى: {top_admin}%)",
        f"خصومات الإحالة: {ref_n} (أعلى: {top_ref}%)",
    ]
    if nearest_end:
        out.append(f"أقرب انتهاء: {nearest_end.isoformat()}")
    return out
