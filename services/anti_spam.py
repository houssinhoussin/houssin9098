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
