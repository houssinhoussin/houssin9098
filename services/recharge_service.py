# services/recharge_service.py
from datetime import datetime
from database.db import get_table
from services.wallet_service import add_balance

RECHARGE_CODES_TABLE = "recharge_codes"

def validate_recharge_code(code):
    # تحقق من الكود في الجدول الفعلي وغير مستخدم
    table = get_table(RECHARGE_CODES_TABLE)
    response = table.select("*").eq("code", code).eq("used", False).limit(1).execute()
    return response.data[0] if response.data else None

def apply_recharge(user_id, code):
    table = get_table(RECHARGE_CODES_TABLE)
    recharge = validate_recharge_code(code)
    if recharge:
        # تحديث حالة الكود إلى مستخدم
        table.update({
            "used": True,
            "used_by": user_id,
            "used_at": datetime.utcnow().isoformat()
        }).eq("id", recharge['id']).execute()
        # شحن الرصيد للمستخدم
        add_balance(user_id, recharge["amount"], "إيداع")
        return recharge["amount"]
    return 0

# (لا تحتاج ملف json أو أكواد ثابتة إطلاقًا، كل شيء من قاعدة البيانات)
