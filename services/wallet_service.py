# services/wallet_service.py
"""
(نفس الهيدر التوضيحي السابق)
"""
# أعلى الملف
import logging
import uuid
from config import SUPABASE_TABLE_NAME
from datetime import datetime, timedelta

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

# خصومات
from services.discount_service import (
    get_active_for_user as _disc_get_active_for_user,
    record_discount_use as _disc_record_use,
)

# أسماء الجداول
USER_TABLE = (SUPABASE_TABLE_NAME or DEFAULT_TABLE or "houssin363")
if USER_TABLE == "USERS_TABLE":
    USER_TABLE = "houssin363"
TRANSACTION_TABLE = "transactions"
PURCHASES_TABLE   = "purchases"
PRODUCTS_TABLE    = "products"
CHANNEL_ADS_TABLE = "channel_ads"


# ================= أدوات مساعدة =================

def _is_uuid_like(x) -> bool:
    """يتحقق إن كانت القيمة تمثل UUID صالحًا."""
    try:
        uuid.UUID(str(x))
        return True
    except Exception:
        return False


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


# ================= المشتريات =================

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
    إدراج شراء مع تطبيق أفضل خصم فعّال للحظة الشراء (يتجاهل المنتهي آليًا).
    نسجّل الحقول الاختيارية لو وُجدت أعمدتها، ون fallback للحقول الأساسية إذا لم توجد.
    ثم نخصم المبلغ النهائي فقط عبر RPC.
    """
    expire_at = datetime.utcnow() + timedelta(hours=15)

    # 1) احسب أفضل خصم فعّال للمستخدم (global/user) – يتجاهل المنتهي
    base_price = int(price)
    final_price = base_price
    disc_id = None
    disc_percent = 0

    d = _disc_get_active_for_user(user_id)
    if d:
        try:
            disc_percent = int(d.get("percent") or 0)
            disc_id = d.get("id")
            cut = (base_price * disc_percent) // 100
            final_price = max(0, base_price - cut)
        except Exception:
            final_price = base_price
            disc_id = None
            disc_percent = 0

    # 2) حاول الإدراج مع الحقول الإضافية؛ وإن فشل أعد الإدراج بالحد الأدنى
    data_full = {
        "user_id": user_id,
        "product_id": product_id,
        "product_name": product_name,
        "price": final_price,                     # السعر بعد الخصم
        "player_id": player_id,
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": expire_at.isoformat(),
        # اختياري:
        "original_price": base_price,
        "discount_percent": disc_percent,
        "discount_id": disc_id,
        "discount_applied_at": datetime.utcnow().isoformat() if disc_id else None,
    }
    try:
        ins = get_table(PURCHASES_TABLE).insert(data_full).execute()
        purchase_id = (ins.data[0]["id"] if getattr(ins, "data", None) else None)
    except Exception:
        # إدراج بالحد الأدنى
        data_min = {
            "user_id": user_id,
            "product_id": product_id,
            "product_name": product_name,
            "price": final_price,
            "player_id": player_id,
            "created_at": datetime.utcnow().isoformat(),
            "expire_at": expire_at.isoformat(),
        }
        ins = get_table(PURCHASES_TABLE).insert(data_min).execute()
        purchase_id = (ins.data[0]["id"] if getattr(ins, "data", None) else None)

    # 3) خصم الرصيد بالمبلغ النهائي فقط
    deduct_balance(user_id, final_price, f"شراء {product_name}")

    # 4) سجّل استخدام الخصم (إن وُجد id)
    if disc_id:
        try:
            _disc_record_use(disc_id, user_id, base_price, final_price, purchase_id)
        except Exception:
            pass


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
# (كما هي)

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
# (كما هي)
def get_all_purchases_structured(user_id: int, limit: int = 50):
    from datetime import datetime as _dt
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


# ===== واجهات الحجز =====

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
