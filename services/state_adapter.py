# services/state_adapter.py
from .state_service import get_state_key, set_state, clear_state

class UserStateDictLike:
    """واجهة بسيطة تشبه القاموس لاستخدامها داخل الـ handlers."""
    def get(self, user_id: int, default=None):
        return get_state_key(user_id, default)

    def __getitem__(self, user_id: int):
        val = get_state_key(user_id, None)
        if val is None:
            raise KeyError(user_id)
        return val

    def __setitem__(self, user_id: int, value: str):
        set_state(user_id, value, ttl_minutes=120)  # افتراضي: ساعتان

    def pop(self, user_id: int, default=None):
        try:
            v = self.get(user_id, default)
            clear_state(user_id)
            return v
        except Exception:
            return default\n
