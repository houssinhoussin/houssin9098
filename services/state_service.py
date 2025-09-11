# services/state_service.py
# -*- coding: utf-8 -*-
"""
تخزين حالة المستخدم لحظة بلحظة في جدول user_state (Supabase/Postgres).

- الحالة النصية تحفظ في vars['__state'] مع وقت انتهاء vars['__state_exp'] (ISO).
- أي حقول إضافية تخص الرحلة تحفظ داخل vars كـ JSONB.
- عند الانتهاء نحذف المفاتيح، وإذا أصبحت vars فارغة نحذف الصف بالكامل.
- يتم ضبط expires_at (عمود الجدول) بالتزامن مع __state_exp لسهولة التنظيف المجدول.
- تعتمد الدوال على قيد UNIQUE(user_id, state_key) لكي يعمل upsert على نحو صحيح.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from database.db import get_table
import logging, time  # ← إضافة

TABLE = "user_state"
DEFAULT_STATE_KEY = "global"

# ===================== Helpers =====================

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python 3.11: fromisoformat supports TZ
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None

def _expires_from_ttl(ttl_minutes: Optional[int]) -> Optional[datetime]:
    if ttl_minutes is None:
        return None
    return _utcnow() + timedelta(minutes=ttl_minutes)

# ===================== Low-level DB =====================

# ← إضافة: منطق إعادة المحاولة مع backoff خفيف
_RETRIES = 3
_BACKOFF = 0.8  # seconds

def _with_retry(op, *args, **kwargs):
    for i in range(_RETRIES):
        try:
            return op(*args, **kwargs)
        except Exception as e:
            if i == _RETRIES - 1:
                raise
            logging.warning("state_service retry %s/%s: %s", i + 1, _RETRIES, e)
            time.sleep(_BACKOFF * (i + 1))

def _select_row(user_id: int, *, state_key: str = DEFAULT_STATE_KEY) -> Optional[Dict[str, Any]]:
    tbl = get_table(TABLE)
    resp = _with_retry(
        tbl.select("*").eq("user_id", user_id).eq("state_key", state_key).limit(1).execute
    )
    data = getattr(resp, "data", None) or []
    return data[0] if data else None

def _upsert_row(user_id: int, *, vars_dict: Dict[str, Any], expires_at: Optional[datetime], state_key: str = DEFAULT_STATE_KEY) -> Dict[str, Any]:
    tbl = get_table(TABLE)
    payload = {
        "user_id": user_id,
        "state_key": state_key,
        "vars": vars_dict,
    }
    if expires_at is not None:
        payload["expires_at"] = _to_iso(expires_at)
    # on_conflict requires UNIQUE(user_id,state_key)
    resp = _with_retry(tbl.upsert(payload, on_conflict="user_id,state_key").execute)
    return getattr(resp, "data", None) or []

def _delete_row(user_id: int, *, state_key: str = DEFAULT_STATE_KEY) -> None:
    tbl = get_table(TABLE)
    _with_retry(tbl.delete().eq("user_id", user_id).eq("state_key", state_key).execute)

# ===================== Vars helpers =====================

def _get_vars(user_id: int, *, state_key: str = DEFAULT_STATE_KEY) -> Dict[str, Any]:
    row = _select_row(user_id, state_key=state_key)
    return dict(row.get("vars") or {}) if row else {}

def _set_vars(user_id: int, vars_dict: Dict[str, Any], *, state_key: str = DEFAULT_STATE_KEY, ttl_minutes: Optional[int] = None) -> None:
    # إذا كانت vars فارغة نحذف الصف تمامًا
    if not vars_dict:
        _delete_row(user_id, state_key=state_key)
        return
    expires_at = _expires_from_ttl(ttl_minutes) or _from_iso(vars_dict.get("__state_exp"))
    _upsert_row(user_id, vars_dict=vars_dict, expires_at=expires_at, state_key=state_key)

# ===================== Public API =====================

def set_kv(user_id: int, key: str, value: Any, *, state_key: str = DEFAULT_STATE_KEY, ttl_minutes: Optional[int] = 120) -> None:
    """خزن مفتاح/قيمة داخل vars، ويُحدِّث وقت الانتهاء."""
    vars_dict = _get_vars(user_id, state_key=state_key)
    vars_dict[key] = value
    if ttl_minutes is not None:
        vars_dict["__state_exp"] = _to_iso(_expires_from_ttl(ttl_minutes))
    _set_vars(user_id, vars_dict, state_key=state_key, ttl_minutes=ttl_minutes)

def get_kv(user_id: int, key: str, default=None, *, state_key: str = DEFAULT_STATE_KEY):
    """اقرأ مفتاحاً من vars مع مراعاة مدة الصلاحية للحالة العامة إذا وُجدت."""
    vars_dict = _get_vars(user_id, state_key=state_key)
    exp = _from_iso(vars_dict.get("__state_exp"))
    if exp and exp < _utcnow():
        # انتهت الصلاحية: امسح الحالة العامة فقط
        vars_dict.pop("__state", None)
        vars_dict.pop("__state_exp", None)
        _set_vars(user_id, vars_dict, state_key=state_key, ttl_minutes=None)
        return default
    return vars_dict.get(key, default)

def set_state(user_id: int, value: str, *, state_key: str = DEFAULT_STATE_KEY, ttl_minutes: int = 120) -> None:
    """اضبط الحالة النصية الحالية."""
    vars_dict = _get_vars(user_id, state_key=state_key)
    vars_dict["__state"] = value
    exp = _expires_from_ttl(ttl_minutes)
    vars_dict["__state_exp"] = _to_iso(exp)
    _set_vars(user_id, vars_dict, state_key=state_key, ttl_minutes=ttl_minutes)

def get_state_key(user_id: int, default=None, *, state_key: str = DEFAULT_STATE_KEY):
    """اقرأ الحالة النصية الحالية؛ ترجع default إن لم توجد أو إن انتهت صلاحيتها."""
    vars_dict = _get_vars(user_id, state_key=state_key)
    exp = _from_iso(vars_dict.get("__state_exp"))
    if exp and exp < _utcnow():
        vars_dict.pop("__state", None)
        vars_dict.pop("__state_exp", None)
        _set_vars(user_id, vars_dict, state_key=state_key, ttl_minutes=None)
        return default
    return vars_dict.get("__state", default)

def clear_state(user_id: int, key: Optional[str] = None, *, state_key: str = DEFAULT_STATE_KEY) -> None:
    """
    لو key=None: احذف الحالة العامة فقط (لا تمس باقي المتغيرات).
    لو محدد key: احذف المفتاح المحدد من vars.
    إذا أصبحت vars فارغة بعد الحذف نحذف الصف بالكامل.
    """
    vars_dict = _get_vars(user_id, state_key=state_key)
    if key is None:
        vars_dict.pop("__state", None)
        vars_dict.pop("__state_exp", None)
    else:
        vars_dict.pop(key, None)
    if vars_dict:
        _set_vars(user_id, vars_dict, state_key=state_key, ttl_minutes=None)
    else:
        _delete_row(user_id, state_key=state_key)

def pop_state(user_id: int, default=None, *, state_key: str = DEFAULT_STATE_KEY):
    val = get_state_key(user_id, default, state_key=state_key)
    clear_state(user_id, state_key=state_key)
    return val


# ==== توافق خلفي مع anti_spam وغيره ====
def get_var(user_id: int, key: str, default=None, *, state_key: str = DEFAULT_STATE_KEY):
    """Alias لـ get_kv لأغراض التوافق."""
    return get_kv(user_id, key, default, state_key=state_key)

def set_var(user_id: int, key: str, value, *, state_key: str = DEFAULT_STATE_KEY, ttl_minutes: int = 120) -> None:
    """Alias لـ set_kv لأغراض التوافق."""
    return set_kv(user_id, key, value, state_key=state_key, ttl_minutes=ttl_minutes)

# ====== واجهة مستوى أعلى لتعامل القاموس بالكامل (باستثناء المفاتيح الداخلية) ======
_INTERNAL_KEYS = {"__state", "__state_exp"}

# ← إضافة: كاش خفيف لقراءات get_data فقط
_CACHE = {}
_CACHE_TTL = 2.0  # ثانيتان

def _cache_key(user_id, state_key):
    return f"{user_id}:{state_key or 'global'}"

def _cache_get(user_id, state_key):
    t, val = _CACHE.get(_cache_key(user_id, state_key), (0, None))
    return val if (time.time() - t) < _CACHE_TTL else None

def _cache_set(user_id, state_key, val):
    _CACHE[_cache_key(user_id, state_key)] = (time.time(), val)

def get_data(user_id: int, *, state_key: str = DEFAULT_STATE_KEY) -> Dict[str, Any]:
    """يرجع نسخة من المتغيرات العامة (بدون مفاتيح النظام)."""
    cached = _cache_get(user_id, state_key)
    if cached is not None:
        return cached
    try:
        row = _select_row(user_id, state_key=state_key)
    except Exception:
        # فشل الشبكة: ارجع حالة فارغة بدل الكراش
        return {}
    vars_dict = dict(row.get("vars") or {}) if row else {}
    result = {k: v for k, v in vars_dict.items() if k not in _INTERNAL_KEYS}
    _cache_set(user_id, state_key, result)
    return result

def set_data(user_id: int, data: Dict[str, Any], *, state_key: str = DEFAULT_STATE_KEY, ttl_minutes: int = 120) -> None:
    """يحفظ القاموس كاملاً مع الإبقاء على مفاتيح النظام كما هي وتحديث وقت الانتهاء."""
    vars_dict = _get_vars(user_id, state_key=state_key)
    # أبقِ مفاتيح النظام
    sys_part = {k: v for k, v in vars_dict.items() if k in _INTERNAL_KEYS}
    merged = {**sys_part, **(data or {})}
    _set_vars(user_id, merged, state_key=state_key, ttl_minutes=ttl_minutes)


# ===== حذف كل حالة المستخدم فورًا (يمسح الصف) =====
def purge_state(user_id: int, *, state_key: str = DEFAULT_STATE_KEY) -> None:
    """Delete the entire state row for this user (no TTL, no leftovers)."""
    _delete_row(user_id, state_key=state_key)


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
