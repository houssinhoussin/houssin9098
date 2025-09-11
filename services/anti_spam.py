# -*- coding: utf-8 -*-
# services/anti_spam.py — Debounce/Rate-limit بسيط على مستوى المستخدم + المسار

from datetime import datetime, timedelta
from services.state_service import get_var, set_var

def _now_iso():
    return datetime.utcnow().isoformat()

def too_soon(user_id: int, key: str, seconds: int = 2) -> bool:
    """يرجع True إذا كانت هناك نقرة/طلب بنفس المفتاح خلال نافذة زمنية قصيرة."""
    k = f"last_action::{key}"
    last = get_var(user_id, k, None)
    now = datetime.utcnow()
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            last_dt = None
        if last_dt and (now - last_dt) <= timedelta(seconds=seconds):
            return True
    # حدّث آخر وقت
    set_var(user_id, k, _now_iso())
    return False
