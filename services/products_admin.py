# services/products_admin.py
from database.db import get_table
from typing import Optional

PRODUCTS_TABLE = "products"

def set_product_active(product_id: int, active: bool) -> bool:
    # جلب details الحالية ثم تعديل active داخل JSON
    resp = get_table(PRODUCTS_TABLE).select("details").eq("id", product_id).limit(1).execute()
    if not resp.data:
        return False
    details = resp.data[0].get("details") or {}
    if not isinstance(details, dict):
        details = {}
    details["active"] = bool(active)
    get_table(PRODUCTS_TABLE).update({"details": details}).eq("id", product_id).execute()
    return True

def is_product_active(details: Optional[dict]) -> bool:
    if not isinstance(details, dict):
        return True  # افتراضياً نشط
    return details.get("active", True)
