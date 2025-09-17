# -*- coding: utf-8 -*-
"""
خدمة المحفظة والمشتريات مع دعم خصمين (أدمن + إحالة) للأنواع المطلوبة.
- تُطبَّق الخصومات على: products, bill_and_units_purchases, university_fees_purchases, internet_providers_purchases
- لا تُطبَّق على: wholesale_purchases
- استثناء خاص: داخل الفواتير، عناصر "جملة كازية سيرياتيل" و"جملة كازية MTN" بلا خصم.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from config import SUPABASE_TABLE_NAME
from database.db import (
    get_table,
    DEFAULT_TABLE,
    # واجهات RPC الذرّية
    get_available_balance as _db_get_available_balance,
    create_hold_rpc as _rpc_create_hold,
    capture_hold_rpc as _rpc_capture_hold,
    release_hold_rpc as _rpc_release_hold,
    transfer_amount_rpc as _rpc_transfer_amount,
    try_deduct_rpc as _rpc_try_deduct,
)

# ===== خصومات (إضافة) =====
# نجمع خصم الأدمن + خصم الإحالة (سقف 100%)، مع فلترة الوقت وتجاهل المنتهي.
from services.discount_service import (
    apply_discount_stacked as _disc_apply_stacked,
    # record_discount_use as _disc_record_use,  # فعّلها لاحقًا إن أردت تتبّع الاستخدام
    get_active_for_user as _disc_get_active_for_user,  # متروكة للتوافق إن استُخدمت بمكان آخر
)

# أسماء الجداول
USER_TABLE = (SUPABASE_TABLE_NAME or DEFAULT_TABLE or "houssin363")
if USER_TABLE == "USERS_TABLE":
    USER_TABLE = "houssin363"
TRANSACTION_TABLE = "transactions"
PURCHASES_TABLE   = "purchases"
PRODUCTS_TABLE    = "products"
CHANNEL_ADS_TABLE = "channel_ads"

# جداول قراءة/كتابة أخرى
BILL_TABLE               = "bill_and_units_purchases"
UNIVERSITY_FEES_TABLE    = "university_fees_purchases"
INTERNET_PROVIDERS_TABLE = "internet_providers_purchases"
WHOLESALE_TABLE          = "wholesale_purchases"

# أنواع يُسمح فيها بالخصم المزدوج
DISCOUNT_ALLOWED_KINDS = {"product", "bill", "university", "internet"}

# ================= أدوات عامة =================

def _is_uuid_like(x) -> bool:
    """يتحقق إن كانت القيمة تمثل UUID صالحًا."""
    try:
        uuid.UUID(str(x))
        return True
    except Exception:
        return False

def _norm(s: str | None) -> str:
    return (s or "").strip().lower()

def _contains_any(text: str, tokens) -> bool:
    t = _norm(text)
    return any(tok in t for tok in tokens)

def _bill_is_excluded(bill_name: str | None = None,
                      provider_name: str | None = None,
                      category: str | None = None) -> bool:
    """
    قواعد الاستثناء داخل الفواتير:
      - وجود أي من مُشغّلي: سيرياتيل/Syriatel أو MTN
      - مع وجود دلالات "جملة" أو "كازية"
      => لا يطبّق خصم.
    نستخدم bill_name فقط إن لم تكن الحقول الأخرى متاحة.
    """
    ops = ["سيرياتيل", "syriatel", "mtn", "ام تي ان", "أم تي أن"]
    bulk = ["جملة", "جمله", "bulk"]
    fuel = ["كازية", "كازي"]

    blob = " ".join([
        _norm(bill_name),
        _norm(provider_name),
        _norm(category),
    ])

    has_op   = _contains_any(blob, ops)
    has_bulk = _contains_any(blob, bulk) or _contains_any(blob, fuel)
    return bool(has_op and has_bulk)

def _apply_discounts_if_allowed(user_id: int, base_price: int, kind: str = "product"):
    """
    يُرجع (final_price, info) حيث info = {"percent":..., "breakdown":[...]}.
    - يطبق خصمين (أدمن + إحالة) فقط إذا kind ضمن DISCOUNT_ALLOWED_KINDS.
    - بخلاف ذلك يعيد السعر كما هو (بدون خصم).
    """
    base_price = int(base_price)
    kind = (kind or "product").lower()
    if kind not in DISCOUNT_ALLOWED_KINDS:
        return base_price, {"percent": 0, "breakdown": []}
    final_price, info = _disc_apply_stacked(user_id, base_price)
    return int(final_price), (info or {"percent": 0, "breakdown": []})


# ================= عمليات المستخدم =================

def register_user_if_not_exist(user_id: int, name: str = "مستخدم") -> None:
    try:
        get_table(USER_TABLE).upsert(
            {"user_id": user_id, "name": name},
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        logging.error(f"[wallet_service] upsert user failed: {e}")
        return

def get_balance(user_id: int) -> int:
    # نُبقيها كما كانت: تُرجع الرصيد الكامل (بدون طرح المحجوز)
    response = (
        get_table(USER_TABLE)
        .select("balance")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return response.data[0]["balance"] if response.data else 0

def get_available_balance(user_id: int) -> int:
    """
    المتاح للصرف = balance - held
    (يُستخدم في التحقق قبل التحويل/الخصم)
    """
    return int(_db_get_available_balance(user_id))

def _update_balance(user_id: int, delta: int) -> bool:
    """
    تعديل الرصيد داخليًا:
      - إن كان delta سالبًا => خصم آمن عبر RPC (try_deduct) يحترم الرصيد المتاح.
      - إن كان delta موجبًا => نُبقي الزيادة كما كانت (قراءة ثم تحديث) لأنها لا تُسبب سالبًا.
    ترجع True عند النجاح، False عند الفشل (مثلاً: عدم كفاية الرصيد).
    """
    if delta is None or int(delta) == 0:
        return True
    delta = int(delta)
    if delta < 0:
        resp = _rpc_try_deduct(user_id, -delta)
        if getattr(resp, "error", None):
            return False
        return bool(resp.data)
    else:
        # زيادة الرصيد (غير حرِجة من ناحية السالب)
        current = get_balance(user_id)
        new_balance = current + delta
        get_table(USER_TABLE).update({"balance": new_balance}).eq("user_id", user_id).execute()
        return True

def has_sufficient_balance(user_id: int, amount: int) -> bool:
    # يعتمد على المتاح (balance - held) حتى لا يتجاوز الحجز
    return get_available_balance(user_id) >= int(amount or 0)

def add_balance(user_id: int, amount: int, description: str = "إيداع يدوي") -> None:
    ok = _update_balance(user_id, int(amount))
    if ok:
        record_transaction(user_id, int(amount), description)

def deduct_balance(user_id: int, amount: int, description: str = "خصم تلقائي") -> None:
    """
    خصم آمن عبر RPC. في حال فشل الخصم (عدم كفاية المتاح) لا تُسجل عملية.
    """
    ok = _update_balance(user_id, -int(amount))
    if ok:
        record_transaction(user_id, -int(amount), description)

def record_transaction(user_id: int, amount: int, description: str) -> None:
    data = {
        "user_id": user_id,
        "amount": int(amount),
        "description": description,
        "timestamp": datetime.utcnow().isoformat(),
    }
    get_table(TRANSACTION_TABLE).insert(data).execute()

def transfer_balance(from_user_id: int, to_user_id: int, amount: int, fee: int = 0) -> bool:
    """
    تحويل رصيد آمن:
      1) نتحقق من المتاح >= المبلغ + الرسوم.
      2) ننفّذ تحويل المبلغ نفسه ذرّيًا (خصم من المرسل + إيداع للمستقبل) عبر RPC transfer_amount.
      3) نخصم الرسوم من المرسل عبر RPC try_deduct (ستنجح طالما تحققنا في (1)).
      4) نسجّل عمليتين ماليّتين بنفس الصياغة القديمة.
    """
    amount  = int(amount)
    fee     = int(fee or 0)
    total   = amount + fee

    if total <= 0:
        return False
    if not has_sufficient_balance(from_user_id, total):
        return False

    # (2) تحويل المبلغ إلى المستقبل
    t = _rpc_transfer_amount(from_user_id, to_user_id, amount)
    if getattr(t, "error", None) or not bool(t.data):
        return False

    # (3) خصم الرسوم من المرسل (إن وجدت)
    if fee > 0:
        f = _rpc_try_deduct(from_user_id, fee)
        if getattr(f, "error", None) or not bool(f.data):
            # غير متوقع بعد التحقق المسبق، لكن نُعيد False للحذر
            return False

    # (4) التسجيلات المحاسبية بنفس الأسلوب السابق
    record_transaction(from_user_id, -total, f"تحويل إلى {to_user_id} (شامل الرسوم)")
    record_transaction(to_user_id,   amount, f"تحويل من {from_user_id}")
    return True


# ================= المشتريات (الأساسي) =================

def get_purchases(user_id: int, limit: int = 10):
    now = datetime.utcnow()
    table = get_table(PURCHASES_TABLE)
    # تنظيف القديمة
    table.delete().eq("user_id", user_id).lt("expire_at", now.isoformat()).execute()
    # جلب الفعّالة فقط
    response = (
        table.select("product_name,price,created_at,player_id,expire_at")
        .eq("user_id", user_id)
        .gt("expire_at", now.isoformat())
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    items = []
    for row in response.data or []:
        ts = (row.get("created_at") or "")[:19].replace("T", " ")
        items.append(f"{row.get('product_name')} ({row.get('price')} ل.س) - آيدي/رقم: {row.get('player_id')} - بتاريخ {ts}")
    return items

def add_purchase(user_id: int, product_id, product_name: str, price: int, player_id: str):
    """
    إدراج شراء قياسي في جدول purchases.
    - يطبق الخصمين (أدمن + إحالة) لكونه نوع 'product'.
    """
    expire_at = datetime.utcnow() + timedelta(hours=15)

    base_price = int(price)
    final_price, info = _apply_discounts_if_allowed(user_id, base_price, kind="product")
    # ملاحظة: يمكن لاحقًا حفظ original_price/discount_percent لو أعمدة purchases تدعمها

    data = {
        "user_id": user_id,
        "product_id": product_id,   # يمكن أن تكون None
        "product_name": product_name,
        "price": final_price,
        "player_id": player_id,
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": expire_at.isoformat(),
    }
    get_table(PURCHASES_TABLE).insert(data).execute()
    deduct_balance(user_id, final_price, f"شراء {product_name}")


# ================= السجلات المالية =================

def get_transfers(user_id: int, limit: int = 10):
    response = (
        get_table(TRANSACTION_TABLE)
        .select("description,amount,timestamp")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    transfers = []
    for row in response.data or []:
        ts = (row.get("timestamp") or "")[:19].replace("T", " ")
        amount = int(row.get("amount") or 0)
        desc = row.get("description") or ""
        transfers.append(f"{desc} ({amount:+,} ل.س) في {ts}")
    return transfers

def get_deposit_transfers(user_id: int, limit: int = 10):
    resp = (
        get_table(TRANSACTION_TABLE)
        .select("description,amount,timestamp")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(200)
        .execute()
    )
    out = []
    for row in (resp.data or []):
        desc = (row.get("description") or "").strip()
        amt  = int(row.get("amount") or 0)
        if amt > 0 and desc.startswith("شحن محفظة"):
            ts = (row.get("timestamp") or "")[:19].replace("T", " ")
            out.append({"description": desc, "amount": amt, "timestamp": ts})
            if len(out) >= limit:
                break
    return out


# ================= المنتجات =================

def get_all_products():
    response = get_table(PRODUCTS_TABLE).select("*").order("id", desc=True).execute()
    return response.data or []

def get_product_by_id(product_id: int):
    response = get_table(PRODUCTS_TABLE).select("*").eq("id", product_id).limit(1).execute()
    return response.data[0] if response.data else None

# مساعد انتقائي
def _select_single(table_name, field, value):
    response = get_table(table_name).select(field).eq(field, value).limit(1).execute()
    return response.data[0][field] if response.data else None


# ================= جداول مشتريات متخصصة (عرض/قراءة) =================

def get_ads_purchases(user_id: int):
    response = get_table('ads_purchases').select("*").eq("user_id", user_id).execute()
    ads_items = []
    for item in response.data or []:
        ads_items.append(f"إعلان: {item['ad_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return ads_items if ads_items else ["لا توجد مشتريات إعلانات."]

def get_bill_and_units_purchases(user_id: int):
    response = get_table('bill_and_units_purchases').select("*").eq("user_id", user_id).execute()
    bills_items = []
    for item in response.data or []:
        bills_items.append(f"فاتورة: {item['bill_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return bills_items if bills_items else ["لا توجد مشتريات فواتير ووحدات."]

def get_cash_transfer_purchases(user_id: int):
    response = get_table('cash_transfer_purchases').select("*").eq("user_id", user_id).execute()
    cash_items = []
    for item in response.data or []:
        cash_items.append(f"تحويل نقدي: {item['transfer_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return cash_items if cash_items else ["لا توجد مشتريات تحويل نقدي."]

def get_companies_transfer_purchases(user_id: int):
    response = get_table('companies_transfer_purchases').select("*").eq("user_id", user_id).execute()
    company_items = []
    for item in response.data or []:
        company_items.append(f"تحويل شركة: {item['company_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return company_items if company_items else ["لا توجد مشتريات تحويلات شركات."]

def get_internet_providers_purchases(user_id: int):
    response = get_table('internet_providers_purchases').select("*").eq("user_id", user_id).execute()
    internet_items = []
    for item in response.data or []:
        internet_items.append(f"مزود إنترنت: {item['provider_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return internet_items if internet_items else ["لا توجد مشتريات مزودي إنترنت."]

def get_university_fees_purchases(user_id: int):
    response = get_table('university_fees_purchases').select("*").eq("user_id", user_id).execute()
    uni_items = []
    for item in response.data or []:
        uni_items.append(f"رسوم جامعة: {item['university_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return uni_items if uni_items else ["لا توجد مشتريات رسوم جامعية."]

def get_wholesale_purchases(user_id: int):
    response = get_table('wholesale_purchases').select("*").eq("user_id", user_id).execute()
    wholesale_items = []
    for item in response.data or []:
        wholesale_items.append(f"جملة: {item['wholesale_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return wholesale_items if wholesale_items else ["لا توجد مشتريات جملة."]

# دالة للتحقق من موافقة الأدمن (تعطيلها بإرجاع True دائماً)
def user_has_admin_approval(user_id):
    return True


# ================= إضافات العرض الموحّد =================

def get_all_purchases_structured(user_id: int, limit: int = 50):
    # (نفس منطقك مع تعديلات تجميلية طفيفة)
    from datetime import datetime as _dt  # لتفادي أي التباس بالأسماء
    items = []

    try:
        resp = (
            get_table(PURCHASES_TABLE)
            .select("id,product_name,price,created_at,player_id")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit * 2)
            .execute()
        )
        for r in (resp.data or []):
            items.append({
                "title": r.get("product_name") or "منتج",
                "price": int(r.get("price") or 0),
                "created_at": r.get("created_at"),
                "id_or_phone": r.get("player_id"),
            })
    except Exception:
        pass

    tables = [
        ("game_purchases", "product_name"),
        ("ads_purchases", "ad_name"),
        ("bill_and_units_purchases", "bill_name"),
        ("cash_transfer_purchases", "transfer_name"),
        ("companies_transfer_purchases", "company_name"),
        ("internet_providers_purchases", "provider_name"),
        ("university_fees_purchases", "university_name"),
        ("wholesale_purchases", "wholesale_name"),
    ]
    probe = ["player_id","phone","number","msisdn","account","account_number","student_id","student_number","target_id","target","line","game_id"]
    for tname, title_field in tables:
        try:
            resp = (
                get_table(tname)
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit * 2)
                .execute()
            )
            for r in (resp.data or []):
                idp = None
                for k in probe:
                    if k in r and r.get(k):
                        idp = r.get(k)
                        break
                items.append({
                    "title": r.get(title_field) or tname,
                    "price": int(r.get("price") or 0),
                    "created_at": r.get("created_at"),
                    "id_or_phone": idp,
                })
        except Exception:
            continue

    def _to_sec(s: str):
        if not s:
            return None
        s2 = s[:19].replace("T", " ")
        try:
            return int(_dt.fromisoformat(s2).timestamp())
        except Exception:
            return None

    seen_lastsec = {}
    uniq = []
    for it in sorted(items, key=lambda x: x.get("created_at") or "", reverse=True):
        key = (it.get("title"), int(it.get("price") or 0), it.get("id_or_phone"))
        sec = _to_sec(it.get("created_at"))
        last = seen_lastsec.get(key)
        if last is not None and sec is not None and abs(sec - last) <= 5:
            continue
        if sec is not None:
            seen_lastsec[key] = sec
        uniq.append(it)
        if len(uniq) >= limit:
            break
    return uniq

def get_wallet_transfers_only(user_id: int, limit: int = 50):
    resp = (
        get_table(TRANSACTION_TABLE)
        .select("description,amount,timestamp")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(300)
        .execute()
    )
    out = []
    last = {}
    for row in (resp.data or []):
        desc = (row.get("description") or "").strip()
        amount = int(row.get("amount") or 0)

        if not ((amount > 0 and desc.startswith("شحن محفظة")) or
                (amount < 0 and desc.startswith("تحويل إلى"))):
            continue

        ts_raw = (row.get("timestamp") or "")[:19].replace("T", " ")
        try:
            dt = datetime.fromisoformat(ts_raw)
            ts_sec = int(dt.timestamp())
        except Exception:
            ts_sec = None

        k = (desc, amount)
        if ts_sec is not None and k in last and abs(ts_sec - last[k]) <= 3:
            continue
        if ts_sec is not None:
            last[k] = ts_sec

        out.append({"description": desc, "amount": amount, "timestamp": ts_raw})
        if len(out) >= limit:
            break
    return out


# ===== تسجيلات إضافية في الجداول المتخصصة (Write-through) =====
# (حافظنا عليها كما في ملفك، مع إضافة تطبيق الخصم حيث لزم)

def add_game_purchase(user_id: int, product_id, product_name: str, price: int, player_id: str, created_at: str = None):
    # تبقى بدون خصم (تكتب في game_purchases فقط)
    pid = int(product_id) if product_id else None
    if pid:
        try:
            chk = get_table(PRODUCTS_TABLE).select("id").eq("id", pid).limit(1).execute()
            if not (getattr(chk, "data", None) and chk.data):
                pid = None
        except Exception:
            pid = None

    data = {
        "user_id": user_id,
        "product_id": pid,
        "product_name": product_name,
        "price": int(price),
        "player_id": str(player_id or ""),
        "created_at": (created_at or datetime.utcnow().isoformat()),
    }
    get_table("game_purchases").insert(data).execute()

def add_bill_or_units_purchase(user_id: int, bill_name: str, price: int, number: str, created_at: str = None):
    # نطبق الخصم (كـ bill) إلا إذا كان استثناء "جملة كازية" مع (سيرياتيل/MTN)
    base_price = int(price)
    if _bill_is_excluded(bill_name=bill_name):
        final_price = base_price
    else:
        final_price, _ = _apply_discounts_if_allowed(user_id, base_price, kind="bill")

    data = {
        "user_id": user_id,
        "bill_name": bill_name,
        "price": final_price,
        "created_at": (created_at or datetime.utcnow().isoformat()),
        "number": number
    }
    try:
        get_table("bill_and_units_purchases").insert(data).execute()
    except Exception:
        pass

def add_internet_purchase(user_id: int, provider_name: str, price: int, phone: str, speed: str = None, created_at: str = None):
    # يُطبق الخصم (كـ internet)
    base_price = int(price)
    final_price, _ = _apply_discounts_if_allowed(user_id, base_price, kind="internet")

    data = {
        "user_id": user_id,
        "provider_name": provider_name,
        "price": final_price,
        "created_at": (created_at or datetime.utcnow().isoformat()),
        "phone": phone,
        "speed": speed
    }
    try:
        get_table("internet_providers_purchases").insert(data).execute()
    except Exception:
        pass

def add_cash_transfer_purchase(user_id: int, transfer_name: str, price: int, number: str, created_at: str = None):
    data = {
        "user_id": user_id,
        "transfer_name": transfer_name,
        "price": price,
        "created_at": (created_at or datetime.utcnow().isoformat()),
        "number": number
    }
    try:
        get_table("cash_transfer_purchases").insert(data).execute()
    except Exception:
        pass

def add_companies_transfer_purchase(user_id: int, company_name: str, price: int, beneficiary_number: str, created_at: str = None):
    data = {
        "user_id": user_id,
        "company_name": company_name,
        "price": price,
        "created_at": (created_at or datetime.utcnow().isoformat()),
        "beneficiary_number": beneficiary_number
    }
    try:
        get_table("companies_transfer_purchases").insert(data).execute()
    except Exception:
        pass

def add_university_fees_purchase(user_id: int, university_name: str, price: int, university_id: str, created_at: str = None):
    # يُطبق الخصم (كـ university)
    base_price = int(price)
    final_price, _ = _apply_discounts_if_allowed(user_id, base_price, kind="university")

    data = {
        "user_id": user_id,
        "university_name": university_name,
        "price": final_price,
        "created_at": (created_at or datetime.utcnow().isoformat()),
        "university_id": university_id
    }
    try:
        get_table("university_fees_purchases").insert(data).execute()
    except Exception:
        pass

def add_ads_purchase(user_id: int, ad_name: str, price: int, created_at: str = None):
    data = {
        "user_id": user_id,
        "ad_name": ad_name,
        "price": price,
        "created_at": (created_at or datetime.utcnow().isoformat())
    }
    try:
        get_table("ads_purchases").insert(data).execute()
    except Exception:
        pass


# ===== واجهات الحجز (للاستخدام من الهاندلرز) =====
# (Back-compat: نقبل UUID أو وصف نصّي، ونمرّر دائمًا UUID صالح للـ RPC)

def create_hold(user_id: int, amount: int, order_or_reason=None, ttl_seconds: int = 900):
    """
    يقبل UUID أو وصف نصّي:
      - لو البراميتر UUID: نستخدمه كـ order_id.
      - لو None أو نص/أي شيء تاني: نولّد UUID جديد ونستخدمه كـ order_id.
    دائمًا نمرّر UUID صالح للـ RPC لتفادي 22P02.
    """
    order_id = str(order_or_reason) if _is_uuid_like(order_or_reason) else str(uuid.uuid4())
    return _rpc_create_hold(user_id, int(amount), order_id, ttl_seconds)

def capture_hold(hold_id: str):
    return _rpc_capture_hold(hold_id)

def release_hold(hold_id: str):
    return _rpc_release_hold(hold_id)
