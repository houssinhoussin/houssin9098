# services/products_admin.py
from __future__ import annotations

from typing import Optional, Dict, Any, List, Tuple
from database.db import get_table

PRODUCTS_TABLE = "products"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tbl():
    """مرجع سريع لجدول المنتجات."""
    return get_table(PRODUCTS_TABLE)

def _safe_dict(value: Any) -> Dict[str, Any]:
    """يضمن أن القيمة قاموس صالح لتخزين JSON."""
    return value if isinstance(value, dict) else {}

# ---------------------------------------------------------------------------
# قراءة/إنشاء الصفوف
# ---------------------------------------------------------------------------

def get_product_row(product_id: int) -> Optional[Dict[str, Any]]:
    """
    يجلب صف المنتج من القاعدة (id, name, category, details).
    يرجع None إذا لم يُعثر على صف.
    """
    try:
        resp = _tbl().select("id,name,category,details").eq("id", product_id).limit(1).execute()
        data = getattr(resp, "data", None)
        return data[0] if data else None
    except Exception:
        # نتجنب انفجار الاستثناءات هنا، ونعيد None ليتصرف النداء الأعلى.
        return None

def ensure_product_row(
    product_id: int,
    name: Optional[str] = None,
    category: Optional[str] = None,
    *,
    default_active: bool = True,
) -> Dict[str, Any]:
    """
    يضمن وجود صف للمنتج. ينشئه عند عدم وجوده.
    يمكن تمرير name/category (مفيدة عند مزامنة المنتجات من الكود).
    """
    row = get_product_row(product_id)
    if row:
        return row

    payload: Dict[str, Any] = {"id": product_id, "details": {"active": bool(default_active)}}
    if name is not None:
        payload["name"] = name
    if category is not None:
        payload["category"] = category

    _tbl().insert(payload).execute()
    # حاول القراءة بعد الإدراج للتأكد من القيمة النهائية المخزنة
    return get_product_row(product_id) or payload

# ---------------------------------------------------------------------------
# حالة التفعيل (Active)
# ---------------------------------------------------------------------------

def is_product_active(details: Optional[dict]) -> bool:
    """
    يقرأ حالة التفعيل من حقل details. الافتراضي True إذا لم توجد قيمة.
    """
    d = _safe_dict(details)
    return bool(d.get("active", True))

def get_product_active(product_id: int, default: bool = True) -> bool:
    """
    يرجع حالة التفعيل لمنتج عبر ID. إن لم يوجد صف، يرجع default.
    """
    row = get_product_row(product_id)
    if not row:
        return bool(default)
    return is_product_active(row.get("details"))

def set_product_active(
    product_id: int,
    active: bool,
    *,
    create_if_missing: bool = True,
) -> bool:
    """
    يحدّث حالة التفعيل داخل details.
    - create_if_missing=True: ينشئ صفاً افتراضياً إذا لم يكن موجوداً (موصى به).
    - عند الفشل يرجع False.
    """
    row = get_product_row(product_id)
    if not row:
        if not create_if_missing:
            return False
        row = ensure_product_row(product_id)

    details = _safe_dict(row.get("details"))
    details["active"] = bool(active)

    try:
        _tbl().update({"details": details}).eq("id", product_id).execute()
        return True
    except Exception:
        return False

def toggle_product_active(product_id: int) -> Optional[bool]:
    """
    يبدّل حالة التفعيل ويعيد الحالة الجديدة (True/False).
    يعيد None إذا فشل التحديث لسبب ما.
    """
    row = get_product_row(product_id) or ensure_product_row(product_id)
    current = is_product_active(row.get("details"))
    new_state = not current
    ok = set_product_active(product_id, new_state, create_if_missing=True)
    return new_state if ok else None

# ---------------------------------------------------------------------------
# عمليات تفاصيل عامة (اختيارية مفيدة)
# ---------------------------------------------------------------------------

def upsert_product_details(
    product_id: int,
    patch: Dict[str, Any],
    *,
    create_if_missing: bool = True,
) -> bool:
    """
    يدمج مفاتيح/قيم جديدة داخل details (Upsert JSON).
    لا يغيّر المفاتيح غير المذكورة في patch.
    """
    row = get_product_row(product_id)
    if not row:
        if not create_if_missing:
            return False
        row = ensure_product_row(product_id)

    details = _safe_dict(row.get("details"))
    details.update(patch or {})

    try:
        _tbl().update({"details": details}).eq("id", product_id).execute()
        return True
    except Exception:
        return False

def bulk_ensure_products(items: List[Tuple[int, str, str]]) -> None:
    """
    يضمن وجود مجموعة من المنتجات دفعة واحدة.
    items: [(id, name, category), ...]
    """
    for pid, name, category in items:
        ensure_product_row(pid, name=name, category=category)
