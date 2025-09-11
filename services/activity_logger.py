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
