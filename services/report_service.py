# services/report_service.py
from datetime import datetime, timedelta
from database.db import get_table

TRANSACTION_TABLE = "transactions"
PURCHASES_TABLE   = "purchases"
PENDING_TABLE     = "pending_requests"

def totals_deposits_and_purchases_syp():
    # إجمالي الإيداعات = مجموع المبالغ الموجبة
    resp = get_table(TRANSACTION_TABLE).select("amount, description").execute()
    dep = 0
    pur = 0
    for r in resp.data or []:
        amt = int(r.get("amount") or 0)
        if amt > 0 and (str(r.get("description") or "").startswith(("إيداع", "تحويل من"))):
            dep += amt
        if amt < 0 and (str(r.get("description") or "").startswith(("شراء", "خصم"))):
            pur += -amt
    # أفضل المنتجات شراءً
    resp2 = get_table(PURCHASES_TABLE).select("product_name").execute()
    counter = {}
    for r in resp2.data or []:
        name = r.get("product_name") or "غير مسمى"
        counter[name] = counter.get(name, 0) + 1
    top = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:5]
    return dep, pur, top

def pending_queue_count():
    resp = get_table(PENDING_TABLE).select("id").eq("status","pending").execute()
    return len(resp.data or [])

def summary(period: str = "day"):
    now = datetime.utcnow()
    if period == "week":
        since = now - timedelta(days=7)
    else:
        since = now - timedelta(days=1)

    # مشتريات
    p = get_table(PURCHASES_TABLE).select("price, created_at").gt("created_at", since.isoformat()).execute()
    total_sales = sum(int(r.get("price") or 0) for r in (p.data or []))
    count_sales = len(p.data or [])

    # إيداعات/تحويلات مالية (موجبة فقط)
    t = get_table(TRANSACTION_TABLE).select("amount, timestamp, description").gt("timestamp", since.isoformat()).execute()
    total_deposits = sum(int(r.get("amount") or 0) for r in (t.data or []) if int(r.get("amount") or 0) > 0)

    # طابور
    q = get_table(PENDING_TABLE).select("id, created_at").gt("created_at", since.isoformat()).execute()
    count_new_requests = len(q.data or [])

    return {
        "since": since.isoformat(),
        "total_sales": total_sales,
        "count_sales": count_sales,
        "total_deposits": total_deposits,
        "count_new_requests": count_new_requests,
    }
