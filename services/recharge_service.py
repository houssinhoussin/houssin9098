# services/recharge_service.py
from datetime import datetime
from database.db import get_table
from services.wallet_service import add_balance

RECHARGE_CODES_TABLE = "recharge_codes"
DEPOSIT_DESC = "شحن محفظة — كود شحن"

def validate_recharge_code(code):
    """
    فحص أولي اختياري: يرجع صف الكود لو موجود وغير مستخدم.
    (مفيد للواجهات، لكن لا تعتمد عليه في الاستهلاك النهائي لأنه غير ذرّي)
    """
    code = (code or "").strip()
    if not code:
        return None
    table = get_table(RECHARGE_CODES_TABLE)
    response = (
        table.select("*")
        .eq("code", code)
        .eq("used", False)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None

def apply_recharge(user_id, code) -> int:
    """
    يستهلك الكود ذرّيًا:
      - يعدّل السجل إلى used=true بشرط used=false
      - يرجّع المبلغ ويضيفه للمستخدم بوصف موحد
    يرجّع 0 لو الكود غير صالح/مستخدم مسبقًا.
    """
    code = (code or "").strip()
    if not code:
        return 0

    table = get_table(RECHARGE_CODES_TABLE)

    # تحديث ذرّي مع شرط used=false — يعيد الصف المحدّث (بما فيه amount) إن نجح
    upd = (
        table.update({
            "used": True,
            "used_by": user_id,
            "used_at": datetime.utcnow().isoformat()
        })
        .eq("code", code)
        .eq("used", False)
        .execute()
    )

    rows = getattr(upd, "data", None) or []
    if not rows:
        # إما الكود مش موجود/غير صالح، أو اتستخدم بالفعل
        return 0

    # لو اتحدث أكتر من صف يبقى فيه مشكلة فريدة بالكود في الجدول
    rec = rows[0]
    amount = int(rec.get("amount") or 0)
    if amount > 0:
        # وصف يبدأ بـ "شحن محفظة" علشان يظهر في سجل الإيداعات
        add_balance(user_id, amount, DEPOSIT_DESC)
    return amount
