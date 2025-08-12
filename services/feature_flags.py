# services/feature_flags.py
from __future__ import annotations
import logging
from typing import Dict, Any, List, Optional
from database.db import get_table

FEATURES_TABLE = "features"

# مفاتيح المزايا (زوّد/قلّل براحتك)
# ✅ تحتوي على جميع الأزرار الرئيسية + الأزرار الفرعية المذكورة بالهاندلرز
FEATURES_SEED: Dict[str, str] = {
    # ===== المحفظة & السجل =====
    "wallet": "المحفظة",
    "wallet_purchases": "مشترياتي",
    "wallet_transfers": "سجل التحويلات",
    "wallet_p2p": "تحويل بين المحافظ",

    # ===== الشحن (القائمة الرئيسية) + الطرق =====
    "wallet_recharge": "شحن المحفظة",      # موجود أصلاً — أبقيناه
    "recharge_syriatel": "شحن — سيرياتيل كاش",
    "recharge_mtn": "شحن — أم تي إن كاش",
    "recharge_sham": "شحن — شام كاش",
    "recharge_payeer": "شحن — Payeer",

    # ===== تحويل كاش (القائمة + الأنواع) =====
    "cash_transfer": "تحويل كاش",          # موجود أصلاً — أبقيناه
    "cash_syriatel": "تحويل إلى سيرياتيل كاش",
    "cash_mtn": "تحويل إلى أم تي إن كاش",
    "cash_sham": "تحويل إلى شام كاش",

    # ===== حوالات شركات (القائمة + الشركات) =====
    "companies_transfer": "حوالات شركات",  # موجود أصلاً — أبقيناه
    "company_alharam": "شركة الهرم",
    "company_alfouad": "شركة الفؤاد",
    "company_shakhashir": "شركة شخاشير",

    # ===== الفواتير والوحدات =====
    "mtn_unit": "وحدات MTN",               # موجود أصلاً — أبقيناه
    "syr_unit": "وحدات Syriatel",          # موجود أصلاً — أبقيناه
    "mtn_bill": "فواتير MTN",              # موجود أصلاً — أبقيناه
    "syr_bill": "فواتير Syriatel",         # موجود أصلاً — أبقيناه

    # ===== الإنترنت (القائمة + المزودين) =====
    "internet": "إنترنت",                   # موجود أصلاً — أبقيناه
    "internet_provider_tarassul": "مزود — تراسل",
    "internet_provider_mtn": "مزود — أم تي إن",
    "internet_provider_syriatel": "مزود — سيرياتيل",
    "internet_provider_aya": "مزود — آية",
    "internet_provider_sawa": "مزود — سوا",
    "internet_provider_rannet": "مزود — رن نت",
    "internet_provider_samanet": "مزود — سما نت",
    "internet_provider_omnia": "مزود — أمنية",
    "internet_provider_nas": "مزود — ناس",
    "internet_provider_hypernet": "مزود — هايبر نت",
    "internet_provider_mts": "مزود — MTS",
    "internet_provider_yara": "مزود — يارا",
    "internet_provider_dunia": "مزود — دنيا",
    "internet_provider_aint": "مزود — آينت",

    # ===== الإعلانات =====
    "ads": "إعلانات",                       # موجود أصلاً — أبقيناه

    # ===== الرسوم الجامعية =====
    "university_fees": "رسوم جامعية",       # موجود أصلاً — أبقيناه

    # ===== الخدمات الإعلامية/السوشيال =====
    "media_services": "خدمات سوشيال/ميديا",
    "media_logo": "خدمة — تصميم لوغو احترافي",
    "media_sm_daily": "خدمة — إدارة ونشر يومي",
    "media_ads_launch": "خدمة — إطلاق حملة إعلانية",
    "media_video_edit": "خدمة — مونتاج فيديو قصير",
    "media_twitter_threads": "خدمة — خيوط تويتر جاهزة",
    "media_voiceover": "خدمة — تعليق صوتي احترافي",
    "media_copywriting": "خدمة — كتابة محتوى تسويقي",

    # ===== المنتجات/الألعاب (قائمة عليا + فئات) =====
    # ملاحظة: عندك تعطيل على مستوى كل منتج بـ get_product_active،
    # لكن نضيف أعلام عامة للأزرار الرئيسية لو حبيت توقف القائمة كلها مؤقتًا.
    "products_menu": "المنتجات",
    "games_menu": "شحن ألعاب و تطبيقات",
    "product_pubg": "فئة — شدات ببجي",
    "product_freefire": "فئة — جواهر فري فاير",
    "product_jawaker": "فئة — جواكر",

    # ===== جملة (لو موجود عندك زر بالقائمة) =====
    "wholesale": "شراء جملة",
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
