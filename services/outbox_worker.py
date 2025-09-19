# -*- coding: utf-8 -*-
# services/outbox_worker.py
from __future__ import annotations
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from database.db import get_table

OUTBOX_TABLE = "notifications_outbox"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _send_one(bot, row: Dict[str, Any]) -> bool:
    user_id = int(row["user_id"])
    text: Optional[str] = (row.get("message") or row.get("text") or "").strip() or None
    photo_id: Optional[str] = (row.get("photo_file_id") or "").strip() or None
    parse_mode: Optional[str] = (row.get("parse_mode") or "HTML") or None

    if photo_id:
        bot.send_photo(user_id, photo_id, caption=text or "", parse_mode=parse_mode)
    else:
        bot.send_message(user_id, text or " ", parse_mode=parse_mode)
    return True

def _tick(bot):
    now = _now_iso()
    try:
        # اجلب رسائل غير مرسلة Scheduled <= now
        res = (
            get_table(OUTBOX_TABLE)
            .select("*")
            .is_("sent_at", None)
            .lte("scheduled_at", now)
            .order("scheduled_at", desc=False)
            .limit(30)
            .execute()
        )
        rows: List[Dict[str, Any]] = res.data or []
        for r in rows:
            ok = False
            try:
                ok = _send_one(bot, r)
            except Exception as e:
                # زد عدد المحاولات وواصل
                tries = int(r.get("tries") or 0) + 1
                get_table(OUTBOX_TABLE).update({"tries": tries}).eq("id", r["id"]).execute()
                continue
            if ok:
                get_table(OUTBOX_TABLE).update({"sent_at": _now_iso()}).eq("id", r["id"]).execute()
    except Exception as e:
        # سجل فقط
        print(f"[outbox_worker] tick error: {e}")

def start_outbox_worker(bot, every_seconds: int = 30):
    """
    عامل إرسال رسائل outbox. يُشغَّل من main.py
    """
    def loop():
        _tick(bot)
        threading.Timer(every_seconds, loop).start()
    # تأخير بسيط لضمان اكتمال تهيئة البوت
    threading.Timer(5, loop).start()

