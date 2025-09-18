# -*- coding: utf-8 -*-
try:
    from services.discount_service import apply_discount_stacked as _apply
except Exception:
    def _apply(uid: int, amt: int): return amt, None

def _has_offer(uid: int) -> tuple[bool, int]:
    try:
        _, info = _apply(int(uid or 0), 100)
        pct = int((info or {}).get("percent", 0))
        return (pct > 0, pct)
    except Exception:
        return (False, 0)

def badge(label: str, uid: int, with_percent: bool = False) -> str:
    ok, pct = _has_offer(uid)
    if not ok:
        return label
    return f"{label} | عرض" + (f" ({pct}٪)" if with_percent else "")
