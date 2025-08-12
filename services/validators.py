# -*- coding: utf-8 -*-
# services/validators.py — توحيد قراءة مبالغ المال كأعداد صحيحة (ليرة)

import re

def parse_amount(text: str, min_value: int = 1, max_value: int = 2_000_000) -> int:
    """حوّل إدخال المستخدم إلى عدد صحيح (ل.س) مع حدود أمان. يزيل أي فراغات/فواصل/رموز."
    مثال: '15,000' -> 15000
    """
    if text is None:
        raise ValueError("amount is required")
    # أزل أي شيء غير أرقام
    digits = re.sub(r"[^0-9]", "", str(text))
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
