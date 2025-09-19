# database/db.py
import os
from typing import Optional, Any, Dict
from dotenv import load_dotenv
from supabase import create_client, Client

# حمّل متغيرات البيئة من .env عند التشغيل المحلي
load_dotenv()

# ---------- إعداد Supabase ----------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_API_KEY")

# اجعل اسم الجدول الافتراضي قادماً من البيئة، مع قيمة افتراضية سليمة USERS_TABLE
# مثال: ضع في البيئة SUPABASE_TABLE_NAME=USERS_TABLE
DEFAULT_TABLE = os.getenv("SUPABASE_TABLE_NAME", "USERS_TABLE")
# fallback ذكي: لو بقيت القيمة USERS_TABLE (اسم عام قديم) استخدم houssin363 حتى يتم تغييرها في .env
if DEFAULT_TABLE == "USERS_TABLE":
    DEFAULT_TABLE = "houssin363"

if not SUPABASE_URL:
    raise RuntimeError("Missing SUPABASE_URL in environment (.env)")
if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY (or SUPABASE_API_KEY) in environment (.env)")
if not SUPABASE_URL.startswith("http"):
    raise RuntimeError("SUPABASE_URL must start with http/https")

# عميل موحّد (Singleton)
_supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")


# ---------- دوال أساسية لا تلمسها بقية الأجزاء ----------
def client() -> Client:
    """أرجِع عميل Supabase الموحد."""
    return _supabase


def get_table(table_name: Optional[str] = None):
    """
    أرجِع كائن الجدول المطلوب.
    لو لم يُمرَّر اسم جدول، نستخدم DEFAULT_TABLE (من البيئة أو الافتراضي).
    """
    name = (table_name or DEFAULT_TABLE)
    if not name:
        raise RuntimeError("No table name provided and SUPABASE_TABLE_NAME is not set.")
    return _supabase.table(name)


def table(table_name: Optional[str] = None):
    """
    بديل مريح: لو ما زوّدت اسم جدول، يستخدم DEFAULT_TABLE.
    """
    name = (table_name or DEFAULT_TABLE)
    if not name:
        raise RuntimeError("No table name provided and SUPABASE_TABLE_NAME is not set.")
    return _supabase.table(name)


# ---------- دوال مساعدة لجدول المستخدمين (USERS_TABLE) ----------
# ملاحظات:
# - هذه الدوال تفترض أن DEFAULT_TABLE يشير لجدول المستخدمين (USERS_TABLE).
# - ما غيّرنا أي استدعاءات موجودة في المشروع؛ هذه فقط أدوات إضافية عند الحاجة.

def get_user_by_id(user_id: int):
    """
    يرجع سجل المستخدم بناءً على user_id
    return: Response من supabase-py (فيه .data و .error)
    """
    return table(DEFAULT_TABLE).select("*").eq("user_id", user_id).limit(1).execute()


def create_user(user_id: int, name: str, balance: int = 0, extra: Optional[Dict[str, Any]] = None):
    """
    ينشئ مستخدم جديد.
    extra: حقول إضافية اختيارية تُدمج مع السجل (مثلاً: {'lang': 'ar'})
    """
    payload: Dict[str, Any] = {"user_id": user_id, "name": name, "balance": balance}
    if extra:
        payload.update(extra)
    return table(DEFAULT_TABLE).insert(payload).execute()


def update_balance(user_id: int, new_balance: int):
    """
    يحدّث الرصيد إلى قيمة محددة.
    """
    return table(DEFAULT_TABLE).update({"balance": new_balance}).eq("user_id", user_id).execute()


def increment_balance(user_id: int, amount: int):
    """
    يضيف/يخصم مبلغ من الرصيد (amount يمكن أن يكون سالبًا).
    """
    res = get_user_by_id(user_id)
    if getattr(res, "error", None):
        return res
    if not getattr(res, "data", None):
        # المستخدم غير موجود
        raise ValueError("User not found")
    current_balance = int(res.data[0].get("balance") or 0)
    return update_balance(user_id, current_balance + int(amount))


def ensure_user(user_id: int, name: str, default_balance: int = 0, extra: Optional[Dict[str, Any]] = None):
    """
    يتأكد من وجود المستخدم؛ إذا غير موجود ينشئه ويرجعه.
    """
    res = get_user_by_id(user_id)
    if getattr(res, "error", None):
        return res
    if res.data:
        return res  # موجود
    # غير موجود -> أنشئه
    return create_user(user_id=user_id, name=name, balance=default_balance, extra=extra)


def get_balance(user_id: int) -> int:
    """
    يرجّع الرصيد كعدد صحيح (0 إذا لا يوجد سجل).
    """
    res = get_user_by_id(user_id)
    if getattr(res, "error", None):
        # في حال الخطأ نرفع استثناء حتى تتعامل معه الطبقة الأعلى (أو بدّل للسلوك المناسب لك)
        raise RuntimeError(f"Supabase error: {res.error}")
    if not res.data:
        return 0
    return int(res.data[0].get("balance") or 0)


# ---------- إضافات آمنة للمحفظة (RPC ذرّية عبر Postgres) ----------
# ملاحظات:
# - هذه الدوال تلتزم تمامًا بما اتفقنا عليه: لا تغيير على الدوال القديمة،
#   فقط واجهات تستدعي الدوال SQL الموجودة: create_hold, capture_hold, release_hold,
#   transfer_amount, try_deduct. كما أضفنا مُلحقًا بسيطًا لقراءة held والمتاح.

def get_wallet(user_id: int):
    """
    يرجع حقول المحفظة الأساسية للمستخدم (balance, held).
    """
    return table(DEFAULT_TABLE).select("balance, held").eq("user_id", user_id).limit(1).execute()


def get_available_balance(user_id: int) -> int:
    """
    المتاح = balance - held
    (يرجع 0 إذا لا يوجد سجل)
    """
    res = get_wallet(user_id)
    if getattr(res, "error", None):
        raise RuntimeError(f"Supabase error: {res.error}")
    if not res.data:
        return 0
    balance = int(res.data[0].get("balance") or 0)
    held = int(res.data[0].get("held") or 0)
    return balance - held


def create_hold_rpc(user_id: int, amount: int, order_id: Optional[str] = None, ttl_seconds: Optional[int] = 900):
    """
    ينشئ حجزًا (hold) يجمّد المبلغ داخل held بشرط أن المتاح يكفي.
    يرجع: Response من supabase-py؛ .data تحوي UUID للحجز عند النجاح.
    """
    params: Dict[str, Any] = {
        "p_user_id": user_id,
        "p_amount": amount,
        "p_order_id": order_id,
        "p_ttl_seconds": ttl_seconds,
    }
    return _supabase.rpc("create_hold", params).execute()


def capture_hold_rpc(hold_id: str):
    """
    يصفّي الحجز (خصم فعلي من balance وفك من held).
    يرجع: Response؛ .data تكون True/False.
    """
    return _supabase.rpc("capture_hold", {"p_hold_id": hold_id}).execute()


def release_hold_rpc(hold_id: str):
    """
    يلغي الحجز (يُنقص held فقط دون خصم).
    يرجع: Response؛ .data تكون True/False.
    """
    return _supabase.rpc("release_hold", {"p_hold_id": hold_id}).execute()


def transfer_amount_rpc(from_user: int, to_user: int, amount: int):
    """
    تحويل رصيد آمن يحترم المتاح فقط (balance - held).
    يرجع: Response؛ .data تكون True/False.
    """
    params = {"p_from_user": from_user, "p_to_user": to_user, "p_amount": amount}
    return _supabase.rpc("transfer_amount", params).execute()


def try_deduct_rpc(user_id: int, amount: int):
    """
    خصم مباشر آمن يحترم المتاح فقط (بدون حجز).
    يرجع: Response؛ .data تكون True/False.
    """
    params = {"p_user_id": user_id, "p_amount": amount}
    return _supabase.rpc("try_deduct", params).execute()
