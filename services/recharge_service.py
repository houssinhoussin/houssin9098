# services/recharge_service.py
from datetime import datetime
import logging

from database.db import get_table
from services.wallet_service import add_balance
from config import TABLE_RECHARGE_CODES as _TABLE_RECHARGE_CODES

# اسم الجدول من config، مع افتراضي للاسم القديم لو غير مضبوط
RECHARGE_CODES_TABLE = _TABLE_RECHARGE_CODES or "recharge_codes"

def validate_recharge_code(code: str):
    """
    يتحقق من وجود الكود وأنه غير مستخدم بعد.
    (إبقاء التوقيع والسلوك كما هو)
    """
    try:
        code = (code or "").strip()
        table = get_table(RECHARGE_CODES_TABLE)
        res = (
            table.select("id, code, amount, used")
                 .eq("code", code)
                 .eq("used", False)
                 .limit(1)
                 .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        logging.error(f"[RECHARGE] validate_recharge_code failed for code={code}: {e}", exc_info=True)
        return None

def apply_recharge(user_id: int, code: str) -> int:
    """
    يفعّل الكود ويشحن رصيد المستخدم بالمبلغ.
    التنفيذ ذرّي: إن كان الكود غير مستخدم → يحدّثه إلى used=True ويعيد الصف المفعَّل.
    يعود بالمبلغ المشحون، أو 0 إذا الكود غير صالح/مستخدم.
    """
    code = (code or "").strip()
    table = get_table(RECHARGE_CODES_TABLE)

    try:
        # تحديث ذرّي مع إرجاع الصف المُحدّث (يمنع السباق واستعمال الكود أكثر من مرة)
        upd = (
            table.update({
                    "used": True,
                    "used_by": user_id,
                    "used_at": datetime.utcnow().isoformat()
                })
                .eq("code", code)
                .eq("used", False)
                .select("id, amount")   # مهم: عشان يرجّع الصف المفعَّل
                .execute()
        )

        row = (upd.data or [None])[0]
        if not row:
            # إما الكود غير موجود أو سبق استعماله
            return 0

        amount = int(row.get("amount") or 0)
        if amount > 0:
            add_balance(user_id, amount, "إيداع")
        return amount

    except Exception as e:
        logging.error(f"[RECHARGE] apply_recharge failed for user={user_id}, code={code}: {e}", exc_info=True)
        return 0
