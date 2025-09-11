
# services/discount_service.py
from __future__ import annotations
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import logging
from database.db import get_table

DISCOUNTS_TABLE = "discounts"
USES_TABLE      = "discount_uses"

def list_discounts(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        res = (get_table(DISCOUNTS_TABLE)
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute())
        return getattr(res, "data", []) or []
    except Exception as e:
        logging.exception("[discounts] list failed: %s", e)
        return []

def create_discount(scope: str, percent: int, user_id: Optional[int] = None, active: bool = True) -> Any:
    scope = scope or "global"
    percent = max(0, min(int(percent or 0), 100))
    row = {"scope": scope, "percent": percent, "active": bool(active)}
    if scope == "user":
        row["user_id"] = int(user_id) if user_id else None
    try:
        return get_table(DISCOUNTS_TABLE).insert(row).execute()
    except Exception as e:
        logging.exception("[discounts] create failed: %s", e)
        raise

def set_discount_active(did: str, active: bool) -> bool:
    try:
        get_table(DISCOUNTS_TABLE).update({"active": bool(active)}).eq("id", did).execute()
        return True
    except Exception as e:
        logging.exception("[discounts] toggle failed: %s", e)
        return False

def get_active_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    """
    يرجع أعلى خصم فعّال ينطبق على المستخدم (خصم خاص للمستخدم، أو خصم عام).
    """
    try:
        res = (get_table(DISCOUNTS_TABLE)
               .select("*")
               .eq("active", True)
               .execute())
        rows = getattr(res, "data", []) or []
    except Exception as e:
        logging.exception("[discounts] get_active_for_user failed: %s", e)
        return None

    best = None
    for r in rows:
        sc = (r.get("scope") or "global").lower()
        if sc == "global":
            ok = True
        elif sc == "user":
            ok = (int(r.get("user_id") or 0) == int(user_id))
        else:
            ok = False
        if not ok:
            continue
        if (best is None) or (int(r.get("percent") or 0) > int(best.get("percent") or 0)):
            best = r
    return best

def apply_discount(user_id: int, amount_syp: int) -> (int, Optional[Dict[str, Any]]):
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
    يرجع نصوص جاهزة للإظهار (تجميعي بسيط).
    """
    try:
        res = get_table(USES_TABLE).select("discount_id, user_id, amount_before, amount_after, created_at").limit(500).execute()
        rows = getattr(res, "data", []) or []
    except Exception:
        rows = []
    if not rows:
        return ["لا يوجد استخدامات."]
    total_saved = sum((int(r.get("amount_before") or 0) - int(r.get("amount_after") or 0)) for r in rows)
    return [f"عدد الاستخدامات: {len(rows)}", f"إجمالي التخفيض: {total_saved:,} ل.س"]
