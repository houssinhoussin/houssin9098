# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List
from database.db import get_table
from services.wallet_service import get_available_balance
try:
    from services.discount_service import apply_discount_stacked as _apply
except Exception:
    def _apply(user_id: int, amount: int):
        return amount, None

def _fmt_syp(n: int) -> str:
    n = int(n or 0)
    return f"{n:,} ل.س"

def build_status_rows(user_id: int, username: str | None = None) -> List[str]:
    rows: List[str] = []
    # الرصيد المتاح
    try:
        avail = get_available_balance(user_id)
        rows.append(f"الرصيد المتاح: {_fmt_syp(avail)}")
    except Exception:
        pass
    # الخصومات الفعالة
    try:
        _, info = _apply(user_id, 100)
        pct = int(info.get("percent", 0)) if info else 0
        rows.append(f"خصم فعال: {pct}٪" if pct else "لا يوجد خصم فعال")
    except Exception:
        pass
    # طلباتك المعلّقة
    try:
        q = (
            get_table("pending_requests")
            .select("id,status")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .execute()
        )
        num = len(q.data or [])
        rows.append(f"طلبات بانتظار المعالجة: {num}")
    except Exception:
        pass
    if not username:
        rows.append("تنبيه: لا يوجد @username لحسابك في تيليغرام.")
    return rows

def send_status_hint(bot, msg) -> None:
    try:
        user_id = msg.from_user.id
        username = msg.from_user.username
    except Exception:
        return
    try:
        lines = build_status_rows(user_id, username)
        if not lines:
            return
        txt = "📊 حالتك الآن:\n" + "\n".join("• " + s for s in lines)
        bot.send_message(msg.chat.id, txt)
    except Exception:
        pass
