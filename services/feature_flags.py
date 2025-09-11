# services/feature_flags.py
from __future__ import annotations
import logging
import re
from typing import Dict, Any, List, Optional
from database.db import get_table

FEATURES_TABLE = "features"

# ==============================
# رسائل ونصوص مساعدة
# ==============================
UNAVAILABLE_MSG = "⛔ نعتذر، «{label}» غير متاح حاليًا بسبب الضغط/الصيانة. سنعمل على إعادته للعمل بأقرب وقت. شكرًا لتفهمك ونأسف على الإزعاج 🤍"

def slugify(s: str) -> str:
    """
    تبسيط نص للمفاتيح: حروف/أرقام عربية أو لاتينية + شرطات.
    لا نحاول تحويل العربية، فقط نحذف الرموز ونوحّد الفراغات لشرطات.
    """
    if not s:
        return ""
    s = str(s).strip()
    # حوّل الفراغات والواصلات المتعددة لواصلة واحدة
    s = re.sub(r"[\s_]+", "-", s)
    # اسمح بالحروف العربية واللاتينية والأرقام والواصلة
    s = re.sub(r"[^0-9A-Za-z\u0600-\u06FF\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-").lower()

# ==============================
# مفاتيح جاهزة للعناصر التفصيلية
# ==============================
def key_product(product_id: int, name: str) -> str:
    """
    مفتاح منتج مفرد (60 شدة/310 جوهرة/120000 توكنز..)
    نستخدم ID لثبات المفتاح، ونخزّن label بالاسم الحالي.
    """
    return f"product:item:{int(product_id)}"

def key_units(carrier: str, qty_label: str) -> str:
    """
    مفتاح باقة وحدات لمشغّل معيّن (MTN/Syriatel) بوسم الكمية.
    مثال: units:mtn:2500-وحدة   —   units:syriatel:1000-وحدة
    """
    return f"units:{slugify(carrier)}:{slugify(qty_label)}"

# ==============================
# البذرة (Features Seed) — أزرار عامة
# ==============================
# ✅ تحتوي على الأزرار الرئيسية + الفرعية الثابتة.
FEATURES_SEED: Dict[str, str] = {
    # ===== المحفظة & السجل =====
    "wallet": "المحفظة",
    "wallet_purchases": "مشترياتي",
    "wallet_transfers": "سجل التحويلات",
    "wallet_p2p": "تحويل بين المحافظ",

    # ===== الشحن (القائمة الرئيسية) + الطرق =====
    "wallet_recharge": "شحن المحفظة",
    "recharge_syriatel": "شحن — سيرياتيل كاش",
    "recharge_mtn": "شحن — أم تي إن كاش",
    "recharge_sham": "شحن — شام كاش",
    "recharge_payeer": "شحن — Payeer",

    # ===== تحويل كاش (القائمة + الأنواع) =====
    "cash_transfer": "تحويل كاش",
    "cash_syriatel": "تحويل إلى سيرياتيل كاش",
    "cash_mtn": "تحويل إلى أم تي إن كاش",
    "cash_sham": "تحويل إلى شام كاش",

    # ===== حوالات شركات (القائمة + الشركات) =====
    "companies_transfer": "حوالات شركات",
    "company_alharam": "شركة الهرم",
    "company_alfouad": "شركة الفؤاد",
    "company_shakhashir": "شركة شخاشير",

    # ===== الفواتير والوحدات (مفاتيح عامة) =====
    "mtn_unit": "وحدات MTN",
    "syr_unit": "وحدات Syriatel",
    "mtn_bill": "فواتير MTN",
    "syr_bill": "فواتير Syriatel",

    # ===== الإنترنت (القائمة + المزودين) =====
    "internet": "إنترنت",
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
    "ads": "إعلانات",

    # ===== الرسوم الجامعية =====
    "university_fees": "رسوم جامعية",

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
    # (تعطيل منتج مفرد يتم عبر key_product الديناميكي)
    "products_menu": "المنتجات",
    "games_menu": "شحن ألعاب و تطبيقات",
    "product_pubg": "فئة — شدات ببجي",
    "product_freefire": "فئة — جواهر فري فاير",
    "product_jawaker": "فئة — جواكر",

    # ===== جملة =====
    "wholesale": "شراء جملة",
}

def _tbl():
    return get_table(FEATURES_TABLE)

# ============================================
# (جديد) قوائم معروفة تُزرع تلقائيًا في Startup
# ============================================
# منتجات الألعاب (مطابقة لتعريفاتك في handlers/products.py)
KNOWN_PRODUCTS: List[Dict[str, Any]] = [
    # PUBG
    {"id": 1, "label": "PUBG — 60 شدة"},
    {"id": 2, "label": "PUBG — 325 شدة"},
    {"id": 3, "label": "PUBG — 660 شدة"},
    {"id": 4, "label": "PUBG — 1800 شدة"},
    {"id": 5, "label": "PUBG — 3850 شدة"},
    {"id": 6, "label": "PUBG — 8100 شدة"},
    # FreeFire
    {"id": 7, "label": "FreeFire — 100 جوهرة"},
    {"id": 8, "label": "FreeFire — 310 جوهرة"},
    {"id": 9, "label": "FreeFire — 520 جوهرة"},
    {"id": 10, "label": "FreeFire — 1060 جوهرة"},
    {"id": 11, "label": "FreeFire — 2180 جوهرة"},
    # Jawaker
    {"id": 12, "label": "Jawaker — 10000 توكنز"},
    {"id": 13, "label": "Jawaker — 15000 توكنز"},
    {"id": 14, "label": "Jawaker — 20000 توكنز"},
    {"id": 15, "label": "Jawaker — 30000 توكنز"},
    {"id": 16, "label": "Jawaker — 60000 توكنز"},
    {"id": 17, "label": "Jawaker — 120000 توكنز"},
]

# باقات الوحدات — نفس القوائم داخل handlers/bill_and_units.py
SYRIATEL_UNIT_PACKS = [
    "1000 وحدة", "1500 وحدة", "2013 وحدة", "3068 وحدة", "4506 وحدة",
    "5273 وحدة", "7190 وحدة", "9587 وحدة", "13039 وحدة",
]
MTN_UNIT_PACKS = [
    "1000 وحدة", "5000 وحدة", "7000 وحدة", "10000 وحدة", "15000 وحدة",
    "20000 وحدة", "23000 وحدة", "30000 وحدة", "36000 وحدة",
]

def _seed_known_details() -> int:
    """
    زرع مفاتيح عناصر المنتجات وباقات الوحدات حتى تظهر في لوحة الأدمن
    ويمكن إيقاف أي خيار بمفرده (660 شدة مثلًا).
    """
    created = 0
    try:
        # منتجات الألعاب
        for item in KNOWN_PRODUCTS:
            k = key_product(item["id"], item["label"])
            if ensure_feature(k, item["label"], default_active=True):
                created += 1

        # وحدات سيرياتيل
        for pack in SYRIATEL_UNIT_PACKS:
            k = key_units("syriatel", pack)
            if ensure_feature(k, f"وحدات Syriatel — {pack}", default_active=True):
                created += 1

        # وحدات MTN
        for pack in MTN_UNIT_PACKS:
            k = key_units("mtn", pack)
            if ensure_feature(k, f"وحدات MTN — {pack}", default_active=True):
                created += 1
    except Exception as e:
        logging.exception("[features] _seed_known_details failed: %s", e)
    return created

# ==============================
# إنشاء/تحديث المفاتيح
# ==============================
def ensure_seed() -> int:
    """يزرع المزايا الافتراضية + المفاتيح التفصيلية المعروفة. يرجع عدد المُنشأ."""
    created = 0
    try:
        # 1) الأساسيات
        for k, label in FEATURES_SEED.items():
            r = _tbl().select("key").eq("key", k).limit(1).execute()
            if not getattr(r, "data", None):
                _tbl().insert({"key": k, "label": label, "active": True}).execute()
                created += 1
            else:
                # تحديث الملصق إن تغيّر
                _tbl().update({"label": label}).eq("key", k).execute()

        # 2) العناصر التفصيلية (الألعاب + الوحدات)
        created += _seed_known_details()
    except Exception as e:
        logging.exception("[features] ensure_seed failed: %s", e)
    return created

def ensure_feature(key: str, label: str, default_active: bool = True) -> bool:
    """
    يضمن وجود مفتاح مخصّص (منتج مفرد/باقة وحدات..). يرجّع True لو تم الإنشاء.
    """
    try:
        r = _tbl().select("key").eq("key", key).limit(1).execute()
        if not getattr(r, "data", None):
            _tbl().insert({"key": key, "label": label, "active": bool(default_active)}).execute()
            return True
        else:
            # حدّث الاسم إن تغيّر
            _tbl().update({"label": label}).eq("key", key).execute()
            return False
    except Exception as e:
        logging.exception("[features] ensure_feature failed (%s): %s", key, e)
        return False

def ensure_bulk(items: List[Dict[str, Any]]) -> int:
    """
    زرع جماعي: items = [{key, label, active?}, ...]
    يرجّع عدد الجديد المُنشأ.
    """
    created = 0
    for it in items:
        k = it.get("key")
        lbl = it.get("label", k)
        act = it.get("active", True)
        if ensure_feature(k, lbl, act):
            created += 1
    return created

# ==============================
# استعلامات الحالة
# ==============================
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

# ==============================
# حُرّاس للاستخدام داخل الهاندلرز
# ==============================
def block_if_disabled(bot, chat_id: int, feature_key: str, label: Optional[str] = None) -> bool:
    """إن كانت الميزة مقفلة يرسل تنويه عام ويرجع True (يعني: قِف)."""
    if is_feature_enabled(feature_key, default=True):
        return False
    lbl = label or FEATURES_SEED.get(feature_key, feature_key)
    try:
        bot.send_message(chat_id, f"⛔ ميزة «{lbl}» غير متاحة حاليًا. سنعيد تفعيلها قريبًا.")
    except Exception:
        pass
    return True

def require_feature_or_alert(bot, chat_id: int, key: str, label: str, default_active: bool = True) -> bool:
    """
    يضمن المفتاح + يفحص تفعيله.
    يرجّع True لو يجب إيقاف الإجراء (غير متاح) بعد إرسال رسالة الاعتذار.
    """
    ensure_feature(key, label, default_active=default_active)
    if is_feature_enabled(key, default=True):
        return False
    try:
        bot.send_message(chat_id, UNAVAILABLE_MSG.format(label=label))
    except Exception:
        pass
    return True

# ==============================
# تجميع/ترتيب للعرض الإداري (اختياري)
# ==============================
def _group_for(key: str, label: str) -> str:
    if key.startswith("product:item:"):
        return "المنتجات — عناصر مفردة"
    if key.startswith("units:mtn:"):
        return "وحدات MTN — باقات"
    if key.startswith("units:syriatel:"):
        return "وحدات Syriatel — باقات"
    if key.startswith("internet_provider_"):
        return "الإنترنت — المزودون"
    if key.startswith("recharge_"):
        return "الشحن — طرق"
    if key.startswith("cash_"):
        return "تحويل كاش — الأنواع"
    if key.startswith("company_"):
        return "حوالات شركات — الشركات"
    # مفاتيح ثابتة شائعة:
    fixed_groups = {
        "wallet": "المحفظة",
        "wallet_purchases": "المحفظة",
        "wallet_transfers": "المحفظة",
        "wallet_p2p": "المحفظة",
        "mtn_unit": "الفواتير/الوحدات — عامة",
        "syr_unit": "الفواتير/الوحدات — عامة",
        "mtn_bill": "الفواتير/الوحدات — عامة",
        "syr_bill": "الفواتير/الوحدات — عامة",
        "internet": "الإنترنت — عام",
        "ads": "الإعلانات",
        "university_fees": "الرسوم الجامعية",
        "media_services": "خدمات الميديا",
        "media_logo": "خدمات الميديا",
        "media_sm_daily": "خدمات الميديا",
        "media_ads_launch": "خدمات الميديا",
        "media_video_edit": "خدمات الميديا",
        "media_twitter_threads": "خدمات الميديا",
        "media_voiceover": "خدمات الميديا",
        "media_copywriting": "خدمات الميديا",
        "products_menu": "المنتجات — قوائم",
        "games_menu": "المنتجات — قوائم",
        "product_pubg": "المنتجات — قوائم",
        "product_freefire": "المنتجات — قوائم",
        "product_jawaker": "المنتجات — قوائم",
        "wholesale": "شراء جملة",
    }
    return fixed_groups.get(key, "أخرى")

def list_features_grouped() -> Dict[str, List[Dict[str, Any]]]:
    """
    يرجّع {اسم المجموعة: [features...]} بترتيب أبجدي حسب label داخل كل مجموعة.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in list_features():
        grp = _group_for(row["key"], row["label"])
        out.setdefault(grp, []).append(row)
    # فرز داخلي
    for grp, items in out.items():
        items.sort(key=lambda r: (str(r.get("label") or ""), str(r.get("key"))))
    return out


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
