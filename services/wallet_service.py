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
        {"user_id": user_id, "name": name},
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

# المشتريات (الأساسي)
def get_purchases(user_id: int, limit: int = 10):
    now = datetime.utcnow()
    table = get_table(PURCHASES_TABLE)
    # تنظيف القديمة
    table.delete().eq("user_id", user_id).lt("expire_at", now.isoformat()).execute()
    # جلب الفعّالة فقط
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
    expire_at = datetime.utcnow() + timedelta(hours=15)
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

# سجل التحويلات المالية (عام)
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

# سجل الإيداعات فقط (اختياري قديم)
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

# مساعد انتقائي
def _select_single(table_name, field, value):
    response = get_table(table_name).select(field).eq(field, value).limit(1).execute()
    return response.data[0][field] if response.data else None

# جداول مشتريات متخصصة (للعرض التجميعي فقط)
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

# ------------------------------------------------------------------
# إضافات للعرض فقط (بدون تغيير المنطق)
# ------------------------------------------------------------------

def get_all_purchases_structured(user_id: int, limit: int = 50):
    """
    تُرجع المشتريات بشكل موحّد من عدة جداول مع إزالة التكرارات عند العرض فقط.
    نُهمِل الـid في مفتاح التمييز حتى لا يظهر نفس الحدث مرتين بفارق ثوانٍ.
    """
    items = []

    # purchases الأساسي
    try:
        resp = (
            get_table(PURCHASES_TABLE)
            .select("id, product_name, price, created_at, player_id")
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

    # بقية الجداول (قراءة فقط للعرض)
    tables = [
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

    def _ts_sec(v):  # حتى الثواني
        return (v or "")[:19]

    # إزالة التكرارات (عرض فقط)
    seen = set()
    uniq = []
    for it in sorted(items, key=lambda x: x.get("created_at") or "", reverse=True):
        key = (it.get("title"), it.get("price"), _ts_sec(it.get("created_at")), it.get("id_or_phone"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
        if len(uniq) >= limit:
            break
    return uniq

def get_wallet_transfers_only(user_id: int, limit: int = 50):
    """
    يُرجع فقط عمليات الإيداع والتحويل بين المحافظ،
    ويُسقط التكرارات المتجاورة لو كانت متطابقة بفارق ≤ 3 ثوانٍ.
    لا يُظهر خصومات الشراء/الخصم التلقائي.
    """
    resp = (
        get_table(TRANSACTION_TABLE)
        .select("description, amount, timestamp")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(300)  # نجلب أكثر ثم نفلتر
        .execute()
    )
    out = []
    last = {}  # (desc, amount) -> آخر توقيت بالثواني
    for row in (resp.data or []):
        desc = (row.get("description") or "").strip()
        if not (desc.startswith("إيداع") or desc.startswith("تحويل")):
            continue  # استبعاد خصومات/شراء
        ts_raw = (row.get("timestamp") or "")[:19].replace("T", " ")
        try:
            dt = datetime.fromisoformat(ts_raw)
            ts_sec = int(dt.timestamp())
        except Exception:
            ts_sec = None
        amount = int(row.get("amount") or 0)
        k = (desc, amount)
        if ts_sec is not None and k in last and abs(ts_sec - last[k]) <= 3:
            continue  # إسقاط تكرارات متجاورة
        if ts_sec is not None:
            last[k] = ts_sec
        out.append({"description": desc, "amount": amount, "timestamp": ts_raw})
        if len(out) >= limit:
            break
    return out
