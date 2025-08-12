# services/state_adapter.py
from collections.abc import MutableMapping
from .state_service import get_data, set_data, set_kv, get_kv, clear_state

class _DictProxy(MutableMapping):
    def __init__(self, user_id: int, ttl_minutes: int = 120):
        self.user_id = user_id
        self.ttl = ttl_minutes

    def __getitem__(self, key):
        data = get_data(self.user_id)
        if key in data:
            return data[key]
        raise KeyError(key)

    def __setitem__(self, key, value):
        set_kv(self.user_id, key, value, ttl_minutes=self.ttl)

    def __delitem__(self, key):
        data = get_data(self.user_id)
        if key in data:
            data.pop(key, None)
            set_data(self.user_id, data, ttl_minutes=self.ttl)
        else:
            raise KeyError(key)

    def __iter__(self):
        return iter(get_data(self.user_id))

    def __len__(self):
        return len(get_data(self.user_id))

    def get(self, key, default=None):
        return get_kv(self.user_id, key, default)

    def setdefault(self, key, default=None):
        current = self.get(key, None)
        if current is None:
            self[key] = default
            return default
        return current

class UserStateDictLike:
    """واجهة مشابهة للقاموس: user_states[user_id]['step'] = '...' يكتب فوراً في القاعدة."""
    def __getitem__(self, user_id: int) -> _DictProxy:
        return _DictProxy(user_id)

    def __setitem__(self, user_id: int, value: dict):
        if not isinstance(value, dict):
            raise TypeError("user_states[user_id] يجب أن يكون dict")
        set_data(user_id, value, ttl_minutes=120)

    def get(self, user_id: int, default=None):
        data = get_data(user_id)
        return data if data else (default if default is not None else {})

    def pop(self, user_id: int, default=None):
        try:
            v = get_data(user_id)
            clear_state(user_id)
            set_data(user_id, {}, ttl_minutes=0)
            return v
        except Exception:
            return default
