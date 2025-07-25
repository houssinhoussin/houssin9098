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

from datetime import datetime, timedelta
from database.db import get_table

# أسماء الجداول
USER_TABLE        = "houssin363"
TRANSACTION_TABLE = "transactions"
PURCHASES_TABLE   = "purchases"
PRODUCTS_TABLE    = "products"
CHANNEL_ADS_TABLE = "channel_ads"

# عمليات المستخدم
def register_user_if_not_exist(user_id: int, name: str = "مستخدم") -> None:
    get_table(USER_TABLE).upsert(
        {
            "user_id": user_id,
            "name": name,
        },
        on_conflict="user_id",
    ).execute()

def get_balance(user_id: int) -> int:
    response = (
        get_table(USER_TABLE)
        .select("balance")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return response.data[0]["balance"] if response.data else 0

def _update_balance(user_id: int, delta: int):
    new_balance = get_balance(user_id) + delta
    get_table(USER_TABLE).update({"balance": new_balance}).eq("user_id", user_id).execute()

def has_sufficient_balance(user_id: int, amount: int) -> bool:
    return get_balance(user_id) >= amount

def add_balance(user_id: int, amount: int, description: str = "إيداع يدوي") -> None:
    _update_balance(user_id, amount)
    record_transaction(user_id, amount, description)

def deduct_balance(user_id: int, amount: int, description: str = "خصم تلقائي") -> None:
    _update_balance(user_id, -amount)
    record_transaction(user_id, -amount, description)

def record_transaction(user_id: int, amount: int, description: str) -> None:
    data = {
        "user_id": user_id,
        "amount": amount,
        "description": description,
        "timestamp": datetime.utcnow().isoformat(),
    }
    get_table(TRANSACTION_TABLE).insert(data).execute()

def transfer_balance(from_user_id: int, to_user_id: int, amount: int, fee: int = 0) -> bool:
    total = amount + fee
    if not has_sufficient_balance(from_user_id, total):
        return False
    deduct_balance(from_user_id, total, f"تحويل إلى {to_user_id} (شامل الرسوم)")
    add_balance(to_user_id, amount, f"تحويل من {from_user_id}")
    return True

# المشتريات
def get_purchases(user_id: int, limit: int = 10):
    # حذف السجلات الأقدم من 36 ساعة عند الاستعلام
    now = datetime.utcnow()
    expire_time = now - timedelta(hours=36)
    table = get_table(PURCHASES_TABLE)
    # حذف القديم
    table.delete().eq("user_id", user_id).lt("expire_at", now.isoformat()).execute()
    # جلب المشتريات الفعالة فقط
    response = (
        table.select("product_name", "price", "created_at", "player_id", "expire_at")
        .eq("user_id", user_id)
        .gt("expire_at", now.isoformat())
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    items = []
    for row in response.data or []:
        ts = row["created_at"][:19].replace("T", " ")
        items.append(f"{row['product_name']} ({row['price']} ل.س) - آيدي/رقم: {row['player_id']} - بتاريخ {ts}")
    return items

def add_purchase(user_id: int, product_id: int, product_name: str, price: int, player_id: str):
    expire_at = datetime.utcnow() + timedelta(hours=36)
    data = {
        "user_id": user_id,
        "product_id": product_id,
        "product_name": product_name,
        "price": price,
        "player_id": player_id,
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": expire_at.isoformat(),
    }
    get_table(PURCHASES_TABLE).insert(data).execute()
    deduct_balance(user_id, price, f"شراء {product_name}")

# سجل التحويلات المالية (كل العمليات المالية)
def get_transfers(user_id: int, limit: int = 10):
    response = (
        get_table(TRANSACTION_TABLE)
        .select("description", "amount", "timestamp")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    transfers = []
    for row in response.data or []:
        ts = row["timestamp"][:19].replace("T", " ")
        amount = row["amount"]
        desc = row["description"]
        transfers.append(f"{desc} ({amount:+,} ل.س) في {ts}")
    return transfers

# سجل الإيداعات فقط (في حال استخدمته في مكان آخر)
def get_deposit_transfers(user_id: int, limit: int = 10):
    response = (
        get_table(TRANSACTION_TABLE)
        .select("description", "amount", "timestamp")
        .eq("user_id", user_id)
        .eq("description", "إيداع")
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    transfers = []
    for row in response.data or []:
        ts = row["timestamp"][:19].replace("T", " ")
        transfers.append(f"{row['description']} ({row['amount']} ل.س) في {ts}")
    return transfers

# المنتجات
def get_all_products():
    response = get_table(PRODUCTS_TABLE).select("*").order("id", desc=True).execute()
    return response.data or []

def get_product_by_id(product_id: int):
    response = get_table(PRODUCTS_TABLE).select("*").eq("id", product_id).limit(1).execute()
    return response.data[0] if response.data else None

# الدالة المطلوبة لتصحيح الاستيراد
def _select_single(table_name, field, value):
    response = get_table(table_name).select(field).eq(field, value).limit(1).execute()
    return response.data[0][field] if response.data else None
