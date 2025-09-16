# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta, timezone
import logging

from database.db import get_table

# محاولة استخدام ساعة المشروع، وإلا فـ fallback
try:
    from utils.time import now as _now  # يُفترض أنها تُرجع UTC-aware datetime
except Exception:
    def _now() -> datetime:
        return datetime.now(timezone.utc)

DISCOUNTS_TABLE = "discounts"
USES_TABLE      = "discount_uses"


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


def list_discounts(limit: int = 100) -> List[Dict[str, Any]]:
    """
    ترجع آخر الخصومات مع حقل computed: effective_active = active and not expired (حتى الآن).
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
    ينشئ خصمًا جديدًا. إن زوّدت hours أو days سيتم ضبط ends_at تلقائيًا.
    لا نلمس خصم الإدمن؛ فقط نضيف دعم الوقت والعلامات الاختيارية.
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

    # مدة الانتهاء: الساعات تسبق الأيام
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
    """إنهاء فوري: يعطّل ويضبط ends_at = الآن."""
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


def _time_window_filter(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """فلترة الوقت: starts_at <= الآن و (ends_at is null أو > الآن)."""
    now = _now()
    ok: List[Dict[str, Any]] = []
    for r in rows:
        st = _parse_dt(r.get("starts_at")) or now
        en = _parse_dt(r.get("ends_at"))
        if st <= now and (en is None or en > now):
            ok.append(r)
    return ok


def get_active_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    """
    يرجع أعلى خصم فعّال وغير منتهٍ للمستخدم (خاص أو عام).
    - لا يراكِم خصمين؛ نختار الأعلى فقط.
    """
    try:
        res = (
            get_table(DISCOUNTS_TABLE)
            .select("*")
            .eq("active", True)
            .execute()
        )
        rows: List[Dict[str, Any]] = getattr(res, "data", []) or []
    except Exception as e:
        logging.exception("[discounts] get_active_for_user failed: %s", e)
        return None

    rows = _time_window_filter(rows)

    best: Optional[Dict[str, Any]] = None
    for r in rows:
        sc = (r.get("scope") or "global").lower()
        if sc == "global":
            ok = True
        elif sc == "user":
            ok = int(r.get("user_id") or 0) == int(user_id)
        else:
            ok = False
        if not ok:
            continue
        if (best is None) or (int(r.get("percent") or 0) > int(best.get("percent") or 0)):
            best = r
    return best


def apply_discount(user_id: int, amount_syp: int) -> Tuple[int, Optional[Dict[str, Any]]]:
    """
    يطبق أعلى خصم فعّال. يرجع (السعر_بعد_الخصم, {id, percent}) أو (السعر, None).
    """
    try:
        d = get_active_for_user(user_id)
        if not d:
            return int(amount_syp), None
        pct = int(d.get("percent") or 0)
        after = int(round(amount_syp * (100 - pct) / 100.0))
        return after, {"id": d.get("id"), "percent": pct}
    except Exception as e:
        logging.exception("[discounts] apply failed: %s", e)
        return int(amount_syp), None


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


def discount_stats(days: int = 30) -> List[str]:
    """
    يرجع نصوصًا تلخيصية بسيطة للاستخدام.
    """
    try:
        res = get_table(USES_TABLE).select(
            "discount_id, user_id, amount_before, amount_after, created_at"
        ).limit(500).execute()
        rows = getattr(res, "data", []) or []
    except Exception:
        rows = []
    if not rows:
        return ["لا يوجد استخدامات."]
    total_saved = sum(
        (int(r.get("amount_before") or 0) - int(r.get("amount_after") or 0))
        for r in rows
    )
    return [f"عدد الاستخدامات: {len(rows)}", f"إجمالي التخفيض: {total_saved:,} ل.س"]
