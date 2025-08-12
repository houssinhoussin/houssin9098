# services/recharge_service.py
from datetime import datetime
from database.db import get_table
from services.wallet_service import add_balance

RECHARGE_CODES_TABLE = "recharge_codes"
DEPOSIT_DESC = "شحن محفظة — كود شحن"

def validate_recharge_code(code):
    """
    فحص أولي اختياري: يرجّع صف الكود لو موجود وغير مستخدم.
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
        .is_("used_by", None)  # زيادة أمان: لم يُستخدم من أي مستخدم
        .limit(1)
        .execute()
    )
    return response.data[0] if getattr(response, "data", None) else None


def apply_recharge(user_id, code) -> int:
    """
    يستهلك الكود بذريّة تامّة:
      - يحدّث السجل إلى used=true بشرط used=false و used_by IS NULL (استهلاك مرّة واحدة).
      - يرجّع المبلغ ويضيفه للمستخدم بوصف موحّد.
    يرجّع 0 لو الكود غير صالح/مستخدم مسبقًا.
    """
    code = (code or "").strip()
    if not code:
        return 0

    table = get_table(RECHARGE_CODES_TABLE)

    # تحديث ذرّي مشروط + إرجاع الصف المعدّل (يشمل amount) في عملية واحدة
    upd = (
        table.update(
            {
                "used": True,
                "used_by": user_id,
                "used_at": datetime.utcnow().isoformat(),
            }
        )
        .eq("code", code)
        .eq("used", False)
        .is_("used_by", None)
        .select("amount")  # نضمن رجوع العمود المطلوب
        .execute()
    )

    rows = getattr(upd, "data", None) or []
    if not rows:
        # إمّا الكود غير موجود/غير صالح، أو تم استخدامه بالفعل
        return 0

    rec = rows[0]
    try:
        amount = int(rec.get("amount") or 0)
    except Exception:
        amount = 0

    if amount <= 0:
        return 0

    # وصف يبدأ بـ "شحن محفظة" لظهور واضح في سجلّ المعاملات
    add_balance(user_id, amount, DEPOSIT_DESC)
    return amount
