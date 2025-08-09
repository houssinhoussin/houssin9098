# utils/retry.py
import time
import logging
from functools import wraps

def retry(exceptions, tries=3, delay=0.5, backoff=2.0, what="operation"):
    """
    Decorator لإعادة المحاولة مع Backoff عند حدوث استثناءات معيّنة.
    - exceptions: استثناء أو Tuple من الاستثناءات التي نعيد المحاولة عندها
    - tries: عدد المحاولات
    - delay: التأخير الأولي (ثوانٍ)
    - backoff: مضاعف التأخير بعد كل محاولة
    - what: اسم العملية لأغراض التسجيل
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _tries, _delay = tries, delay
            for attempt in range(1, _tries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    logging.warning(f"[retry] Attempt {attempt}/{_tries} for {what} failed: {e!r}")
                    if attempt == _tries:
                        logging.error(f"[retry] All {tries} attempts for {what} failed.", exc_info=True)
                        raise
                    time.sleep(_delay)
                    _delay *= backoff
        return wrapper
    return decorator
