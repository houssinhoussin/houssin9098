# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

_last_actions = {}

def too_soon(user_id: int, key: str, seconds: int = 2) -> bool:
    now = datetime.utcnow()
    k = (int(user_id), str(key))
    last = _last_actions.get(k)
    if last and (now - last) <= timedelta(seconds=seconds):
        return True
    _last_actions[k] = now
    return False
