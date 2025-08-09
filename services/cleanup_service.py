# services/cleanup_service.py
from datetime import datetime, timedelta
from database.db import get_table

def delete_inactive_users():
    table_users = get_table("houssin363")
    users = table_users.select("user_id, created_at").execute().data
    cutoff = datetime.utcnow() - timedelta(days=30)

    for user in users:
        user_id = user['user_id']
        # تحقق من وجود عمليات شراء أو إيداع في آخر 30 يوم
        purchases = get_table("purchases").select("id").eq("user_id", user_id).gte("created_at", cutoff.isoformat()).execute().data
        deposits = get_table("transactions").select("id").eq("user_id", user_id).eq("description", "إيداع").gte("timestamp", cutoff.isoformat()).execute().data
        if not purchases and not deposits:
            table_users.delete().eq("user_id", user_id).execute()
            print(f"تم حذف المستخدم غير النشيط: {user_id}")

# يمكنك تشغيلها يومياً تلقائياً عبر cron أو كود باكجراوند.
