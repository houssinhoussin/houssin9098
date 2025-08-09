# services/cleanup_service.py
from datetime import datetime, timedelta
import logging
from typing import Optional

from database.db import get_table
from config import (
    TABLE_USERS,
    TABLE_PURCHASES,
    TABLE_TRANSACTIONS,
    TABLE_USER_STATE,
)

INACTIVE_DAYS = 30  # نفس القيمة التي كانت في كودك

def _iso(dt: datetime) -> str:
    return dt.isoformat()

def _parse_dt(v) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None

def delete_inactive_users():
    """
    يحذف المستخدمين الذين ليس لديهم مشتريات ولا إيداعات خلال آخر 30 يومًا.
    (يحافظ على منطق كودك الأصلي، مع لوج أفضل وأسماء جداول من config)
    """
    cutoff = datetime.utcnow() - timedelta(days=INACTIVE_DAYS)
    cutoff_iso = _iso(cutoff)

    try:
        users_res = get_table(TABLE_USERS).select("user_id, created_at").execute()
        users = users_res.data or []
    except Exception as e:
        logging.error(f"[CLEANUP] فشل جلب المستخدمين: {e}", exc_info=True)
        return

    deleted_count = 0
    for user in users:
        user_id = user.get("user_id")
        if not user_id:
            continue

        try:
            purchases = (
                get_table(TABLE_PURCHASES)
                .select("id")
                .eq("user_id", user_id)
                .gte("created_at", cutoff_iso)
                .execute()
            ).data or []

            deposits = (
                get_table(TABLE_TRANSACTIONS)
                .select("id")
                .eq("user_id", user_id)
                .eq("description", "إيداع")
                .gte("timestamp", cutoff_iso)
                .execute()
            ).data or []

            if not purchases and not deposits:
                get_table(TABLE_USERS).delete().eq("user_id", user_id).execute()
                deleted_count += 1
                logging.info(f"[CLEANUP] تم حذف المستخدم غير النشيط: {user_id}")

        except Exception as e:
            logging.error(f"[CLEANUP] خطأ أثناء التحقق/الحذف للمستخدم {user_id}: {e}", exc_info=True)

    logging.info(f"[CLEANUP] اكتمل. عدد المحذوفين: {deleted_count}")

def cleanup_expired_states():
    """
    (اختياري) تنظيف حالات user_state المنتهية صلاحيتها.
    يحذف أي صف expires_at < الآن.
    """
    now_iso = datetime.utcnow().isoformat()
    try:
        (
            get_table(TABLE_USER_STATE)
            .delete()
            .lt("expires_at", now_iso)
            .execute()
        )
        logging.info("[CLEANUP] تم تنظيف حالات user_state المنتهية.")
    except Exception as e:
        logging.error(f"[CLEANUP] فشل تنظيف حالات user_state: {e}", exc_info=True)
