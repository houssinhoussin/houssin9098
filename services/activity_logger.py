# services/activity_logger.py
import json, os, datetime

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "admin_actions.log")

def log_action(admin_id: int, action: str, reason: str = ""):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    rec = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "admin_id": int(admin_id),
        "action": action,
        "reason": reason or "",
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


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
