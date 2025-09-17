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
    get_available_balance as _db_get_available_balance,
    create_hold_rpc as _rpc_create_hold,
    capture_hold_rpc as _rpc_capture_hold,
    release_hold_rpc as _rpc_release_hold,
    transfer_amount_rpc as _rpc_transfer_amount,
    try_deduct_rpc as _rpc_try_deduct,
)

# خصومات
from services.discount_service import (
    apply_discount_stacked as _disc_apply_stacked,   # خصم أدمن + إحالة (سقف 100%)
    record_discount_use as _disc_record_use,         # احتياطي لو أردت تسجيل الاستخدام
    get_active_for_user as _disc_get_active_for_user # للتوافق في أماكن أخرى
)

# أسماء الجداول
USER_TABLE = (SUPABASE_TABLE_NAME or DEFAULT_TABLE or "houssin363")
if USER_TABLE == "USERS_TABLE":
    USER_TABLE = "houssin363"
TRANSACTION_TABLE = "transactions"
PURCHASES_TABLE   = "purchases"
PRODUCTS_TABLE    = "products"

# جداول أخرى للقراءة/العرض
CHANNEL_ADS_TABLE            = "channel_ads"
BILL_TABLE                   = "bill_and_units_purchases"
UNIVERSITY_FEES_TABLE        = "university_fees_purchases"
INTERNET_PROVIDERS_TABLE     = "internet_providers_purchases"
WHOLESALE_TABLE              = "wholesale_purchases"

# أنواع يُسمح فيها بالخصم المزدوج
DISCOUNT_ALLOWED_KINDS = {"product", "bill", "university", "internet"}
# جداول مستثناة بالكامل
DISCOUNT_EXCLUDED_TABLES = {WHOLESALE_TABLE}


# ================= أدوات عامة =================

def _is_uuid_like(x) -> bool:
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
    """
    ops = ["سيرياتيل", "syriatel", "mtn", "ام تي ان", "أم تي أن"]
    bulk = ["جملة", "جمله", "bulk"]
    fuel = ["كازية", "كازي"]

    blob = " ".join([_norm(bill_name), _norm(provider_name), _norm(category)])

    has_op   = _contains_any(blob, ops)
    has_bulk = _contains_any(blob, bulk) or _contains_any(blob, fuel)

    return bool(has_op and has_bulk)


# ================= رصيد ومحفظة =================

def get_balance(user_id: int) -> int:
    response = (
        get_table(USER_TABLE)
        .select("balance")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return response.data[0]["balance"] if response.data else 0

def get_available_balance(user_id: int) -> int:
    return int(_db_get_available_balance(user_id))

def _update_balance(user_id: int, delta: int) -> bool:
    if delta is None or int(delta) == 0:
        return True
    delta = int(delta)
    if delta < 0:
        resp = _rpc_try_deduct(user_id, -delta)
        if getattr(resp, "error", None):
            return False
        return bool(resp.data)
    else:
        current = get_balance(user_id)
        new_balance = current + delta
        get_table(USER_TABLE).update({"balance": new_balance}).eq("user_id", user_id).execute()
        return True

def has_sufficient_balance(user_id: int, amount: int) -> bool:
    return get_available_balance(user_id) >= int(amount or 0)

def record_transaction(user_id: int, amount: int, description: str) -> None:
    data = {
        "user_id": user_id,
        "amount": int(amount),
        "description": description,
        "timestamp": datetime.utcnow().isoformat(),
    }
    get_table(TRANSACTION_TABLE).insert(data).execute()

def add_balance(user_id: int, amount: int, description: str = "شحن محفظة") -> None:
    if _update_balance(user_id, int(amount)):
        record_transaction(user_id, int(amount), description)

def deduct_balance(user_id: int, amount: int, description: str = "خصم تلقائي") -> None:
    if _update_balance(user_id, -int(amount)):
        record_transaction(user_id, -int(amount), description)


# ================= تحويل الرصيد =================

def transfer_balance(from_user_id: int, to_user_id: int, amount: int, fee: int = 0) -> bool:
    amount  = int(amount)
    fee     = int(fee or 0)
    total   = amount + fee

    if total <= 0:
        return False
    if not has_sufficient_balance(from_user_id, total):
        return False

    t = _rpc_transfer_amount(from_user_id, to_user_id, amount)
    if getattr(t, "error", None) or not bool(t.data):
        return False

    if fee > 0:
        f = _rpc_try_deduct(from_user_id, fee)
        if getattr(f, "error", None) or not bool(f.data):
            return False

    record_transaction(from_user_id, -total, f"تحويل إلى {to_user_id} (شامل الرسوم)")
    record_transaction(to_user_id,   amount, f"تحويل من {from_user_id}")
    return True


# ================= مساعدة الخصومات =================

def _apply_discounts_if_allowed(user_id: int, base_price: int, kind: str = "product"):
    """
    يُرجع (final_price, info) حيث info = {"percent":..., "breakdown":[...]}
    - يطبق خصمين (أدمن + إحالة) فقط إذا kind ضمن DISCOUNT_ALLOWED_KINDS.
    - بخلاف ذلك يعيد السعر كما هو (بدون خصم).
    """
    base_price = int(base_price)
    kind = (kind or "product").lower()

    if kind not in DISCOUNT_ALLOWED_KINDS:
        return base_price, {"percent": 0, "breakdown": []}

    final_price, info = _disc_apply_stacked(user_id, base_price)
    return int(final_price), (info or {"percent": 0, "breakdown": []})


# ================= المشتريات: منتجات =================

def add_purchase(user_id: int, product_id, product_name: str, price: int, player_id: str, kind: str = "product"):
    """
    إدراج شراء قياسي في جدول purchases.
    - يطبق الخصمين (أدمن + إحالة) لو kind ∈ {'product','bill','university','internet'}.
    - لا يطبّق خصم للجُملة.
    """
    base_price = int(price)
    expire_at = datetime.utcnow() + timedelta(hours=15)

    # خصم مزدوج عند السماح
    final_price, info = _apply_discounts_if_allowed(user_id, base_price, kind)
    total_percent = int(info.get("percent") or 0)

    data_full = {
        "user_id": user_id,
        "product_id": product_id,
        "product_name": product_name,
        "price": final_price,
        "player_id": player_id,
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": expire_at.isoformat(),
        # اختيارية:
        "original_price": base_price,
        "discount_percent": total_percent,
        "discount_id": None,  # لدينا أكثر من خصم محتمل؛ نتركه None
        "discount_applied_at": datetime.utcnow().isoformat() if total_percent > 0 else None,
    }

    # إدراج مرن: إن فشل بسبب أعمدة غير موجودة، نُعيد الإدراج بدون الأعمدة الاختيارية
    try:
        get_table(PURCHASES_TABLE).insert(data_full).execute()
    except Exception:
        data_min = {
            "user_id": user_id,
            "product_id": product_id,
            "product_name": product_name,
            "price": final_price,
            "player_id": player_id,
            "created_at": datetime.utcnow().isoformat(),
            "expire_at": expire_at.isoformat(),
        }
        get_table(PURCHASES_TABLE).insert(data_min).execute()

    # خصم الرصيد بالمبلغ النهائي فقط
    deduct_balance(user_id, final_price, f"شراء {product_name}")


# ================= الفواتير =================

def add_bill_and_units_purchase(user_id: int,
                                bill_name: str,
                                price: int,
                                account_number: str,
                                provider_name: str | None = None,
                                category: str | None = None):
    """
    إدراج فاتورة في bill_and_units_purchases:
      - يُطبّق خصمين (أدمن + إحالة) إلا إذا كانت الفاتورة من نوع "جملة كازية" مع (سيرياتيل/MTN) → ممنوع خصم.
    """
    base_price = int(price)

    # استثناء "جملة كازية" لمشغلي سيرياتيل/MTN
    if _bill_is_excluded(bill_name=bill_name, provider_name=provider_name, category=category):
        final_price = base_price
    else:
        final_price, _ = _apply_discounts_if_allowed(user_id, base_price, kind="bill")

    data = {
        "user_id": user_id,
        "bill_name": bill_name,
        "price": final_price,
        "account_number": account_number,
        "provider_name": provider_name,
        "category": category,
        "created_at": datetime.utcnow().isoformat(),
    }
    get_table(BILL_TABLE).insert(data).execute()
    deduct_balance(user_id, final_price, f"سداد فاتورة {bill_name}")


# ================= الجامعة =================

def add_university_fee_purchase(user_id: int,
                                university_name: str,
                                price: int,
                                student_number: str):
    """
    إدراج رسوم جامعة مع تطبيق الخصمين (أدمن + إحالة).
    """
    base_price = int(price)
    final_price, _ = _apply_discounts_if_allowed(user_id, base_price, kind="university")

    data = {
        "user_id": user_id,
        "university_name": university_name,
        "price": final_price,
        "student_number": student_number,
        "created_at": datetime.utcnow().isoformat(),
    }
    get_table(UNIVERSITY_FEES_TABLE).insert(data).execute()
    deduct_balance(user_id, final_price, f"رسوم جامعة {university_name}")


# ================= الإنترنت =================

def add_internet_provider_purchase(user_id: int,
                                   provider_name: str,
                                   price: int,
                                   account_number: str):
    """
    إدراج شراء مزوّد إنترنت مع تطبيق الخصمين (أدمن + إحالة).
    """
    base_price = int(price)
    final_price, _ = _apply_discounts_if_allowed(user_id, base_price, kind="internet")

    data = {
        "user_id": user_id,
        "provider_name": provider_name,
        "price": final_price,
        "account_number": account_number,
        "created_at": datetime.utcnow().isoformat(),
    }
    get_table(INTERNET_PROVIDERS_TABLE).insert(data).execute()
    deduct_balance(user_id, final_price, f"اشتراك إنترنت {provider_name}")


# ================= قراءات/عروض (كما كانت تقريبًا) =================

def get_purchases(user_id: int, limit: int = 10):
    now = datetime.utcnow()
    table = get_table(PURCHASES_TABLE)
    table.delete().eq("user_id", user_id).lt("expire_at", now.isoformat()).execute()
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


def get_all_products():
    response = get_table(PRODUCTS_TABLE).select("*").order("id", desc=True).execute()
    return response.data or []

def get_product_by_id(product_id: int):
    response = get_table(PRODUCTS_TABLE).select("*").eq("id", product_id).limit(1).execute()
    return response.data[0] if response.data else None


# ================= حجز الرصيد (RPC) =================

def create_hold(user_id: int, amount: int, order_or_reason=None, ttl_seconds: int = 900):
    order_id = str(order_or_reason) if _is_uuid_like(order_or_reason) else str(uuid.uuid4())
    return _rpc_create_hold(user_id, int(amount), order_id, ttl_seconds)

def capture_hold(hold_id: str):
    return _rpc_capture_hold(hold_id)

def release_hold(hold_id: str):
    return _rpc_release_hold(hold_id)
