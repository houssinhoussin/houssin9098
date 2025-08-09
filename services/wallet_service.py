# services/wallet_service.py
"""
------------------------------------------------------------------
🔸 جداول قاعدة البيانات (Supabase) المعتمدة 🔸
------------------------------------------------------------------

-- 1) جدول المستخدمين houssin363
CREATE TABLE public.houssin363 (
  uuid        uuid        PRIMARY KEY      DEFAULT gen_random_uuid(),
  user_id     int8 UNIQUE,
  name        text,
  balance     int4        DEFAULT 0,
  purchases   jsonb       DEFAULT '[]'::jsonb,
  created_at  timestamptz DEFAULT now()
);

-- 2) جدول الحركات المالية transactions
CREATE TABLE public.transactions (
  id          bigserial   PRIMARY KEY,
  user_id     int8        REFERENCES public.houssin363(user_id) ON DELETE CASCADE,
  amount      int4        NOT NULL,
  description text,
  timestamp   timestamptz DEFAULT now()
);

-- 3) جدول المشتريات purchases
CREATE TABLE public.purchases (
  id           int8 PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  user_id      int8,
  product_id   int8 REFERENCES public.products(id),
  product_name text,
  price        int4,
  created_at   timestamptz DEFAULT now(),
  player_id    text,
  expire_at    timestamptz
);

-- 4) جدول المنتجات products
CREATE TABLE public.products (
  id          int8 PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  name        text,
  type        text,
  details     jsonb,
  created_at  timestamptz DEFAULT now()
);

-- 5) جدول الطابور pending_requests
CREATE TABLE public.pending_requests (
  id           int8 PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  user_id      int8,
  username     text,
  request_text text,
  created_at   timestamptz DEFAULT now(),
  status       text        DEFAULT 'pending',
  payload      jsonb
);
------------------------------------------------------------------
"""

import logging
from datetime import datetime, timedelta

from database.db import get_table
from config import (
    TABLE_USERS as _TABLE_USERS,
    TABLE_TRANSACTIONS as _TABLE_TRANSACTIONS,
    TABLE_PURCHASES as _TABLE_PURCHASES,
    TABLE_PRODUCTS as _TABLE_PRODUCTS,
    TABLE_CHANNEL_ADS as _TABLE_CHANNEL_ADS,
)

# أسماء الجداول (مع افتراضات افتراضية إذا لم تكن موجودة في config)
USER_TABLE        = _TABLE_USERS or "houssin363"
TRANSACTION_TABLE = _TABLE_TRANSACTIONS or "transactions"
PURCHASES_TABLE   = _TABLE_PURCHASES or "purchases"
PRODUCTS_TABLE    = _TABLE_PRODUCTS or "products"
CHANNEL_ADS_TABLE = _TABLE_CHANNEL_ADS or "channel_ads"

# -------------------------------------------------
# أدوات وقت بسيطة
# -------------------------------------------------
def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def _fmt_ts(ts: str) -> str:
    try:
        return (ts or "")[:19].replace("T", " ")
    except Exception:
        return str(ts)

# -------------------------------------------------
# عمليات المستخدم
# -------------------------------------------------
def register_user_if_not_exist(user_id: int, name: str = "مستخدم") -> None:
    try:
        get_table(USER_TABLE).upsert(
            {"user_id": user_id, "name": name},
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        logging.error(f"[WALLET] register_user_if_not_exist failed for {user_id}: {e}", exc_info=True)

def get_balance(user_id: int) -> int:
    try:
        res = (
            get_table(USER_TABLE)
            .select("balance")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            # أنشئ صف للمستخدم إن لم يوجد
            register_user_if_not_exist(user_id)
            return 0
        return int(res.data[0].get("balance") or 0)
    except Exception as e:
        logging.error(f"[WALLET] get_balance failed for {user_id}: {e}", exc_info=True)
        return 0

def _update_balance(user_id: int, delta: int):
    try:
        # نقرأ الرصيد ثم نكتب الجديد
        current = get_balance(user_id)
        new_balance = int(current) + int(delta)
        get_table(USER_TABLE).update({"balance": new_balance}).eq("user_id", user_id).execute()
        return new_balance
    except Exception as e:
        logging.error(f"[WALLET] _update_balance failed for {user_id}: {e}", exc_info=True)
        raise

def has_sufficient_balance(user_id: int, amount: int) -> bool:
    return get_balance(user_id) >= int(amount)

def record_transaction(user_id: int, amount: int, description: str) -> None:
    try:
        data = {
            "user_id": user_id,
            "amount": int(amount),
            "description": description,
            "timestamp": _now_iso(),
        }
        get_table(TRANSACTION_TABLE).insert(data).execute()
    except Exception as e:
        logging.error(f"[WALLET] record_transaction failed for {user_id}: {e}", exc_info=True)

def add_balance(user_id: int, amount: int, description: str = "إيداع يدوي") -> None:
    try:
        register_user_if_not_exist(user_id)
        _update_balance(user_id, int(amount))
        record_transaction(user_id, int(amount), description)
    except Exception as e:
        logging.error(f"[WALLET] add_balance failed for {user_id}: {e}", exc_info=True)

def deduct_balance(user_id: int, amount: int, description: str = "خصم تلقائي") -> None:
    try:
        register_user_if_not_exist(user_id)
        _update_balance(user_id, -int(amount))
        record_transaction(user_id, -int(amount), description)
    except Exception as e:
        logging.error(f"[WALLET] deduct_balance failed for {user_id}: {e}", exc_info=True)

def transfer_balance(from_user_id: int, to_user_id: int, amount: int, fee: int = 0) -> bool:
    try:
        amount = int(amount)
        fee = int(fee)
        total = amount + fee
        if not has_sufficient_balance(from_user_id, total):
            return False
        deduct_balance(from_user_id, total, f"تحويل إلى {to_user_id} (شامل الرسوم)")
        add_balance(to_user_id, amount, f"تحويل من {from_user_id}")
        return True
    except Exception as e:
        logging.error(f"[WALLET] transfer_balance failed {from_user_id}->{to_user_id}: {e}", exc_info=True)
        return False

# -------------------------------------------------
# المشتريات
# -------------------------------------------------
def get_purchases(user_id: int, limit: int = 10):
    """
    يعيد قائمة نصوص لآخر المشتريات النشطة (غير المنتهية).
    يحذف المنتهي (expire_at < الآن) قبل الجلب.
    """
    try:
        now_iso = _now_iso()
        table = get_table(PURCHASES_TABLE)
        # حذف المنتهي
        table.delete().eq("user_id", user_id).lt("expire_at", now_iso).execute()
        # جلب النشط
        res = (
            table.select("product_name, price, created_at, player_id, expire_at")
            .eq("user_id", user_id)
            .gt("expire_at", now_iso)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        items = []
        for row in res.data or []:
            ts = _fmt_ts(row.get("created_at", ""))
            items.append(f"{row.get('product_name','')} ({int(row.get('price') or 0)} ل.س) - آيدي/رقم: {row.get('player_id','')} - بتاريخ {ts}")
        return items
    except Exception as e:
        logging.error(f"[WALLET] get_purchases failed for {user_id}: {e}", exc_info=True)
        return []

def add_purchase(user_id: int, product_id: int, product_name: str, price: int, player_id: str):
    """
    تُنشئ عملية شراء وتخصم السعر من المحفظة.
    ⚠️ تذكير: هذه الدالة تقوم بالخصم داخليًا (كما في كودك الأصلي).
    """
    try:
        expire_at = datetime.utcnow() + timedelta(hours=15)
        data = {
            "user_id": user_id,
            "product_id": product_id,
            "product_name": product_name,
            "price": int(price),
            "player_id": player_id,
            "created_at": _now_iso(),
            "expire_at": expire_at.isoformat(),
        }
        get_table(PURCHASES_TABLE).insert(data).execute()
        # الخصم النهائي
        deduct_balance(user_id, int(price), f"شراء {product_name}")
    except Exception as e:
        logging.error(f"[WALLET] add_purchase failed for {user_id} ({product_name}): {e}", exc_info=True)

# -------------------------------------------------
# سجل التحويلات المالية
# -------------------------------------------------
def get_transfers(user_id: int, limit: int = 10):
    try:
        res = (
            get_table(TRANSACTION_TABLE)
            .select("description, amount, timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        transfers = []
        for row in res.data or []:
            ts = _fmt_ts(row.get("timestamp", ""))
            amount = int(row.get("amount") or 0)
            desc = row.get("description", "")
            transfers.append(f"{desc} ({amount:+,} ل.س) في {ts}")
        return transfers
    except Exception as e:
        logging.error(f"[WALLET] get_transfers failed for {user_id}: {e}", exc_info=True)
        return []

def get_deposit_transfers(user_id: int, limit: int = 10):
    try:
        res = (
            get_table(TRANSACTION_TABLE)
            .select("description, amount, timestamp")
            .eq("user_id", user_id)
            .eq("description", "إيداع")
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        transfers = []
        for row in res.data or []:
            ts = _fmt_ts(row.get("timestamp", ""))
            transfers.append(f"{row.get('description','')} ({int(row.get('amount') or 0)} ل.س) في {ts}")
        return transfers
    except Exception as e:
        logging.error(f"[WALLET] get_deposit_transfers failed for {user_id}: {e}", exc_info=True)
        return []

# -------------------------------------------------
# المنتجات
# -------------------------------------------------
def get_all_products():
    try:
        res = get_table(PRODUCTS_TABLE).select("*").order("id", desc=True).execute()
        return res.data or []
    except Exception as e:
        logging.error(f"[WALLET] get_all_products failed: {e}", exc_info=True)
        return []

def get_product_by_id(product_id: int):
    try:
        res = (
            get_table(PRODUCTS_TABLE)
            .select("*")
            .eq("id", product_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        logging.error(f"[WALLET] get_product_by_id failed for {product_id}: {e}", exc_info=True)
        return None

# -------------------------------------------------
# الدالة المطلوبة لتصحيح الاستيراد (كما هي)
# -------------------------------------------------
def _select_single(table_name, field, value):
    res = (
        get_table(table_name)
        .select(field)
        .eq(field, value)
        .limit(1)
        .execute()
    )
    return res.data[0][field] if res.data else None

# -------------------------------------------------
# دوال تقارير مشتريات إضافية (كما هي)
# -------------------------------------------------
def get_ads_purchases(user_id: int):
    res = get_table('ads_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"إعلان: {item['ad_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات إعلانات."]

def get_bill_and_units_purchases(user_id: int):
    res = get_table('bill_and_units_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"فاتورة: {item['bill_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات فواتير ووحدات."]

def get_cash_transfer_purchases(user_id: int):
    res = get_table('cash_transfer_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"تحويل نقدي: {item['transfer_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات تحويل نقدي."]

def get_companies_transfer_purchases(user_id: int):
    res = get_table('companies_transfer_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"تحويل شركة: {item['company_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات تحويلات شركات."]

def get_internet_providers_purchases(user_id: int):
    res = get_table('internet_providers_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"مزود إنترنت: {item['provider_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات مزودي إنترنت."]

def get_university_fees_purchases(user_id: int):
    res = get_table('university_fees_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"رسوم جامعة: {item['university_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات رسوم جامعية."]

def get_wholesale_purchases(user_id: int):
    res = get_table('wholesale_purchases').select("*").eq("user_id", user_id).execute()
    items = []
    for item in res.data or []:
        items.append(f"جملة: {item['wholesale_name']} ({item['price']} ل.س) - تاريخ: {item['created_at']}")
    return items if items else ["لا توجد مشتريات جملة."]

# -------------------------------------------------
# دالة للتحقق من موافقة الأدمن (كما في كودك)
# -------------------------------------------------
def user_has_admin_approval(user_id):
    return True
