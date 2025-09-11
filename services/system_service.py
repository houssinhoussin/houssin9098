# services/system_service.py
import json, os, time
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "system_state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "bot.log")

def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def set_maintenance(on: bool, message: Optional[str] = None):
    st = _load_state()
    st["maintenance"] = bool(on)
    if message is not None:
        st["maintenance_message"] = message
    _save_state(st)

def is_maintenance() -> bool:
    return bool(_load_state().get("maintenance"))

def maintenance_message() -> str:
    return _load_state().get("maintenance_message") or "🛠️ نعمل على صيانة سريعة الآن. جرّب لاحقًا."

def get_logs_tail(max_lines: int = 30) -> str:
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
        return "".join(lines) or "لا يوجد سجلات بعد."
    except Exception:
        return "لا يمكن قراءة السجلات."

# ملاحظة: يعتمد إعادة التحقق من الاشتراك على كاش مشروعك. هنا فقط نضع إشارة زمنية لمسح أي كاش داخلي.
def force_sub_recheck():
    st = _load_state()
    st["force_sub_epoch"] = int(time.time())
    _save_state(st)
    return st["force_sub_epoch"]


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
