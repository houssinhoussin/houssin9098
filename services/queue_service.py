# services/queue_service.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx

from config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger("queue_service")


# --------------------- HTTP helpers ---------------------
def _http_client() -> httpx.Client:
    # http2=False لثبات أفضل على بعض البيئات (مثل Render)
    return httpx.Client(timeout=20.0, http2=False, transport=httpx.HTTPTransport())


def _rest_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }


def _table_url(tbl: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{tbl}"


# --------------------- Queue API ---------------------
def add_pending_request(
    user_id: int,
    action: str,
    payload: Optional[dict] = None,
    approve_channel: str = "admin",
    meta: Optional[dict] = None,
) -> dict:
    """
    يحاول إدراج طلب في pending_requests؛
    إذا لم يوجد الجدول أو فشل الإدراج، يسقط إلى notifications_outbox.
    يرجّع تمثيل الصف المُدرَج أو كائن نتيجة الفشل/السقوط.
    """
    row = {
        "user_id": int(user_id),
        "action": str(action),
        "payload": payload or {},
        "approve_channel": approve_channel,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "meta": meta or {},
    }

    try:
        with _http_client() as client:
            # المحاولة الأولى: pending_requests
            try:
                r = client.post(
                    _table_url("pending_requests"),
                    headers=_rest_headers(),
                    json=row,
                    params={},  # يمكن إضافة on_conflict إذا عرّفت مفتاحًا فريدًا
                )
                if r.status_code == 404:
                    raise FileNotFoundError("pending_requests table missing")
                r.raise_for_status()
                js = r.json()
                return js[0] if isinstance(js, list) and js else row
            except Exception as e1:
                # سقوط: notifications_outbox
                fb = {
                    "kind": "admin_request",
                    "payload": row,
                    "scheduled_at": datetime.utcnow().isoformat(),
                }
                try:
                    r2 = client.post(
                        _table_url("notifications_outbox"),
                        headers=_rest_headers(),
                        json=fb,
                        params={},
                    )
                    r2.raise_for_status()
                    js2 = r2.json()
                    return {
                        "fallback": "notifications_outbox",
                        "row": js2[0] if isinstance(js2, list) and js2 else fb,
                    }
                except Exception as e2:
                    logger.warning("add_pending_request failed: %s / %s", e1, e2)
                    return {"error": str(e2), "row": row}
    except Exception as e:
        logger.exception("add_pending_request fatal: %s", e)
        return {"error": str(e), "row": row}


def process_queue(*args, **kwargs) -> bool:
    """واجهة مستقبلية لمعالجة الطابور (placeholder)."""
    return True


def delete_pending_request(request_id: int) -> bool:
    try:
        with _http_client() as client:
            r = client.delete(
                _table_url("pending_requests"),
                headers=_rest_headers(),
                params={"id": f"eq.{int(request_id)}"},
            )
            r.raise_for_status()
            return True
    except Exception as e:
        logger.warning("delete_pending_request failed: %s", e)
        return False


def postpone_request(request_id: int, minutes: int = 10) -> bool:
    try:
        new_ts = (datetime.utcnow() + timedelta(minutes=int(minutes))).isoformat()
        with _http_client() as client:
            r = client.patch(
                _table_url("pending_requests"),
                headers=_rest_headers(),
                params={"id": f"eq.{int(request_id)}"},
                json={"scheduled_at": new_ts},
            )
            r.raise_for_status()
            return True
    except Exception as e:
        logger.warning("postpone_request failed: %s", e)
        return False


def queue_cooldown_start(key: str, seconds: int = 60) -> bool:
    """
    يضع قفلًا بسيطًا في app_state (key='cooldown:<key>') لمدة محددة.
    يتطلب جدول app_state (الأعمدة: key text primary key, value jsonb).
    """
    try:
        until = datetime.utcnow().timestamp() + int(seconds)
        with _http_client() as client:
            # upsert على key
            r = client.post(
                _table_url("app_state"),
                headers=_rest_headers(),
                params={"on_conflict": "key"},
                json={"key": f"cooldown:{key}", "value": {"until": until}},
            )
            if r.status_code >= 400:
                # PATCH بديل
                r = client.patch(
                    _table_url("app_state"),
                    headers=_rest_headers(),
                    params={"key": f"eq.cooldown:{key}"},
                    json={"value": {"until": until}},
                )
            r.raise_for_status()
            return True
    except Exception as e:
        logger.warning("queue_cooldown_start failed: %s", e)
        return False
