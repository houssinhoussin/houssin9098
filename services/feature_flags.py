# services/feature_flags.py
from __future__ import annotations
import logging
from typing import Dict, Any, List, Optional
from database.db import get_table

FEATURES_TABLE = "features"

# مفاتيح المزايا (زوّد/قلّل براحتك)
FEATURES_SEED: Dict[str, str] = {
    "wallet_recharge": "شحن المحفظة",
    "cash_transfer": "تحويل كاش",
    "companies_transfer": "حوالات شركات",
    "mtn_unit": "وحدات MTN",
    "syr_unit": "وحدات Syriatel",
    "mtn_bill": "فواتير MTN",
    "syr_bill": "فواتير Syriatel",
    "internet": "إنترنت",
    "ads": "إعلانات",
    "university_fees": "رسوم جامعية",
    # "orders": "طلبات المنتجات الرقمية",  # عادةً نستخدم إيقاف المنتج بدلاً من إيقاف الكل
}

def _tbl():
    return get_table(FEATURES_TABLE)

def ensure_seed() -> int:
    """يزرع المزايا الافتراضية إن لم تكن موجودة. يرجع عدد المُنشأ."""
    created = 0
    try:
        for k, label in FEATURES_SEED.items():
            r = _tbl().select("key").eq("key", k).limit(1).execute()
            if not getattr(r, "data", None):
                _tbl().insert({"key": k, "label": label, "active": True}).execute()
                created += 1
            else:
                # تحديث الملصق إن تغيّر
                _tbl().update({"label": label}).eq("key", k).execute()
    except Exception as e:
        logging.exception("[features] ensure_seed failed: %s", e)
    return created

def list_features() -> List[Dict[str, Any]]:
    try:
        r = _tbl().select("key,label,active").order("label", desc=False).execute()
        return getattr(r, "data", []) or []
    except Exception as e:
        logging.exception("[features] list_features failed: %s", e)
        return []

def set_feature_active(key: str, active: bool) -> bool:
    try:
        _tbl().update({"active": bool(active)}).eq("key", key).execute()
        return True
    except Exception as e:
        logging.exception("[features] set_feature_active failed: %s", e)
        return False

def is_feature_enabled(key: str, default: bool = True) -> bool:
    try:
        r = _tbl().select("active").eq("key", key).limit(1).execute()
        data = getattr(r, "data", None)
        if not data:
            return default
        return bool(data[0].get("active", default))
    except Exception:
        return default

# حارس بسيط للاستعمال داخل الهاندلرز
def block_if_disabled(bot, chat_id: int, feature_key: str, label: Optional[str] = None) -> bool:
    """إن كانت الميزة مقفلة يرسل تنويه ويرجع True (يعني: قِف)."""
    if is_feature_enabled(feature_key, default=True):
        return False
    lbl = label or FEATURES_SEED.get(feature_key, feature_key)
    try:
        bot.send_message(chat_id, f"⛔ ميزة «{lbl}» غير متاحة حاليًا. سنعيد تفعيلها قريبًا.")
    except Exception:
        pass
    return True
