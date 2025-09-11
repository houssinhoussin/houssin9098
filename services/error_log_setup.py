# services/error_log_setup.py
import logging, sys, threading, os
from logging.handlers import RotatingFileHandler

_installed = False

def install_global_error_logging(log_dir: str = "logs", log_file: str = "bot.log", level: int = logging.INFO):
    global _installed
    if _installed:
        return
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, log_file)
    root = logging.getLogger()
    root.setLevel(level)

    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(path, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
        root.addHandler(fh)

    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
        root.addHandler(sh)

    try:
        logging.getLogger("telebot").setLevel(logging.INFO)
    except Exception:
        pass

    def _excepthook(t, e, tb):
        logging.getLogger("global").exception("Unhandled exception", exc_info=(t, e, tb))

    def _threadhook(args):
        logging.getLogger("thread").exception("Unhandled thread exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _threadhook
    _installed = True
