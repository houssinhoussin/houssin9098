# -*- coding: utf-8 -*-
# services/ban_service.py — حظر/فكّ الحظر + فحص الحالة
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, Tuple
from database.db import get_table

TABLE = "banned_users"

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def is_banned(user_id: int) -> Tuple[bool, Optional[str], Optional[str]]:
    """يرجع (محظور؟, banned_until_iso or None, reason)"""
    try:
        res = get_table(TABLE).select("reason, banned_until").eq("user_id", int(user_id)).limit(1).execute()
        row = (getattr(res, "data", None) or [None])[0]
        if not row:
            return (False, None, None)
        until = row.get("banned_until")
        if until:
            try:
                # Supabase يرجع توقيت ISO — نقارنه الآن
                dt = datetime.fromisoformat(until.replace("Z","+00:00"))
                if dt <= _now_utc():
                    # انتهى الحظر — اعتبره غير محظور (يمكن تنظيف السجل لاحقًا)
                    return (False, None, None)
            except Exception:
                pass
        return (True, until, row.get("reason"))
    except Exception:
        # في حال فشل الاستعلام، لا نمنع المستخدم (سلوك متسامح)
        return (False, None, None)

def ban_user(user_id: int, by_admin: int, reason: str, banned_until_iso: Optional[str] = None):
    payload = {
        "user_id": int(user_id),
        "banned_by": int(by_admin),
        "reason": reason or "",
        "banned_until": banned_until_iso,
        "created_at": _now_utc().isoformat(),
    }
    # upsert على user_id
    return get_table(TABLE).upsert(payload, on_conflict="user_id").execute()

def unban_user(user_id: int, by_admin: int):
    # إزالة السجل بالكامل
    return get_table(TABLE).delete().eq("user_id", int(user_id)).execute()


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
