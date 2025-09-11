# -*- coding: utf-8 -*-
# services/validators.py — توحيد قراءة مبالغ المال كأعداد صحيحة (ليرة)

import re

def parse_amount(text: str, min_value: int = 1, max_value: int = 5_000_000) -> int:
    """حوّل إدخال المستخدم إلى عدد صحيح (ل.س) مع حدود أمان. يزيل أي فراغات/فواصل/رموز."
    مثال: '5,000' -> 5000
    """
    if text is None:
        raise ValueError("amount is required")
    # أزل أي شيء غير أرقام
    digits = re.sub(r"[^\d]", "", str(text)).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if digits == "":
        raise ValueError("invalid amount")
    amount = int(digits)
    if amount < min_value:
        raise ValueError(f"min {min_value}")
    if amount > max_value:
        raise ValueError(f"max {max_value}")
    return amount

def fmt_syp(n: int) -> str:
    return f"{int(n):,} ل.س"


def parse_user_id(text: str) -> int:
    """تحويل آيدي المستخدم من نص (يدعم الأرقام العربية) إلى int."""
    if text is None:
        raise ValueError("user_id is required")
    # اسمح بصيغة <code>123</code> أو نص فيه أرقام فقط
    import re
    s = re.sub(r"[^\d]", "", str(text)).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if not s:
        raise ValueError("invalid user_id")
    return int(s)

def parse_duration_choice(choice: str) -> int | None:
    """خرّج مدة الحظر بالثواني من خيار زر: 1d / 7d / perm -> ثواني أو None (دائم)."""
    c = (choice or "").strip().lower()
    if c in ("perm", "permanent"):
        return None
    if c.endswith("d") and c[:-1].isdigit():
        return int(c[:-1]) * 86400
    if c.endswith("h") and c[:-1].isdigit():
        return int(c[:-1]) * 3600
    if c.endswith("m") and c[:-1].isdigit():
        return int(c[:-1]) * 60
    raise ValueError("invalid duration choice")


@bot.message_handler(commands=['cancel'])
def cancel_cmd(m):
    try:
        for dct in (globals().get('_msg_by_id_pending', {}),
                    globals().get('_disc_new_user_state', {}),
                    globals().get('_admin_manage_user_state', {}),
                    globals().get('_address_state', {}),
                    globals().get('_phone_state', {})):
            try:
                dct.pop(m.from_user.id, None)
            except Exception:
                pass
    except Exception:
        pass
    try:
        bot.reply_to(m, "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.")
    except Exception:
        bot.send_message(m.chat.id, "✅ تم الإلغاء.")
