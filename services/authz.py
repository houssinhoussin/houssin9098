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


@bot.message_handler(commands=['cancel'])
def cancel_cmd(m):
    try:
        for dct in (globals().get('_msg_by_id_pending', {}),
                    globals().get('_disc_new_user_state', {}),
                    globals().get('_admin_manage_user_state', {}),
                    globals().get('_address_state', {}),
                    globals().get('_phone_state', {})):
            try:
                dct.pop(m.from_user.id, None)
            except Exception:
                pass
    except Exception:
        pass
    try:
        bot.reply_to(m, "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.")
    except Exception:
        bot.send_message(m.chat.id, "✅ تم الإلغاء.")
