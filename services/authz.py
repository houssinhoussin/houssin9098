# services/authz.py
from __future__ import annotations
from typing import Set
from config import ADMINS, ADMIN_MAIN_ID

PRIMARY_ID = ADMIN_MAIN_ID
SECONDARY_IDS = [i for i in ADMINS if i != PRIMARY_ID]

ACTIONS: dict[str, Set[str]] = {
    # إدارة المستخدم
    "user:message_by_id": {"primary", "secondary"},
    "user:ban": {"primary"},
    "user:unban": {"primary"},
    # طابور الطلبات
    "queue:message": {"primary", "secondary"},
    "queue:photo":   {"primary", "secondary"},
    "queue:confirm": {"primary"},
    "queue:cancel":  {"primary", "secondary"},
    # محفظة
    "wallet:topup":  {"primary"},
    "wallet:deduct": {"primary"},
    # إعلانات
    "ads:post":      {"primary", "secondary"},
    "ads:delete":    {"primary"},
}

def role_of(user_id: int) -> str | None:
    if user_id == PRIMARY_ID:
        return "primary"
    if user_id in ADMINS:
        return "secondary"
    return None

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def is_primary_admin(user_id: int) -> bool:
    return user_id == PRIMARY_ID

def allowed(user_id: int, action: str) -> bool:
    r = role_of(user_id)
    return bool(r and (r in ACTIONS.get(action, set())))
