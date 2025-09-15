# services/referral_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta, timezone

import logging
from telebot import apihelper
from database.db import get_table
from services.discount_service import create_discount, set_discount_active
from config import FORCE_SUB_CHANNEL_ID, CHANNEL_USERNAME, BOT_USERNAME

GOALS_TBL = "referral_goals"
JOINS_TBL = "referral_joins"
DISCOUNTS_TBL = "discounts"

ONE_DAY = timedelta(hours=24)

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _ok_member_status(s: str) -> bool:
    return s in ("member", "administrator", "creator")

def _is_member(bot, user_id: int) -> bool:
    """
    فحص الاشتراك المباشر عبر get_chat_member.
    """
    try:
        m = bot.get_chat_member(FORCE_SUB_CHANNEL_ID, int(user_id))
        st = getattr(m, "status", None)
        return _ok_member_status(st)
    except apihelper.ApiTelegramException as e:
        logging.warning(f"[referral] get_chat_member failed for {user_id}: {e}")
        return False
    except Exception as e:
        logging.exception(f"[referral] membership check error: {e}")
        return False

# ---------- أهداف اليوم ----------

def get_or_create_today_goal(referrer_id: int,
                             required_count: int = 2,
                             ttl: timedelta = ONE_DAY) -> Dict[str, Any]:
    """
    يعيد هدف اليوم للمستخدم؛ أو ينشئه إن لم يوجد.
    """
    now = _now()
    expires = now + ttl
    # هل لديه هدف غير منتهي اليوم؟
    res = (get_table(GOALS_TBL)
           .select("*")
           .eq("referrer_id", referrer_id)
           .in_("status", ["open", "satisfied", "redeemed"])
           .order("created_at", desc=True)
           .limit(1)
           .execute())
    rows = getattr(res, "data", []) or []
    if rows:
        g = rows[0]
        # إن انتهت صلاحيته، نرجعه لكن كمعلومات فقط (لن يُستخدم)
        if g.get("expires_at"):
            try:
                ends = datetime.fromisoformat(str(g["expires_at"]).replace("Z","+00:00"))
            except Exception:
                ends = None
            if ends and ends <= now:
                # سنتركه كما هو، وننشئ هدفاً جديداً بالأسفل
                rows = []
            else:
                return g

    # أنشئ هدفًا جديدًا
    payload = {
        "referrer_id": int(referrer_id),
        "channel_id": int(FORCE_SUB_CHANNEL_ID),
        "required_count": int(required_count),
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "status": "open",
        "meta": {"channel_username": CHANNEL_USERNAME, "bot": BOT_USERNAME},
    }
    g = get_table(GOALS_TBL).insert(payload).execute()
    data = getattr(g, "data", []) or []
    return data[0] if data else payload

def goal_progress(goal_id: str) -> Tuple[int, int, bool]:
    """يعيد (verified_count, required_count, is_satisfied)."""
    res = get_table("referral_progress").select("*").eq("goal_id", goal_id).limit(1).execute()
    rows = getattr(res, "data", []) or []
    if not rows:
        # fallback في حال عدم وجود الـ view
        gq = get_table(GOALS_TBL).select("*").eq("id", goal_id).limit(1).execute()
        g = (getattr(gq, "data", []) or [{}])[0]
        rq = get_table(JOINS_TBL).select("id, verified_at, still_member").eq("goal_id", goal_id).execute()
        cnt = sum(1 for r in (getattr(rq, "data", []) or []) if r.get("verified_at") and r.get("still_member"))
        req = int(g.get("required_count") or 2)
        return cnt, req, cnt >= req
    r = rows[0]
    cnt = int(r.get("verified_count") or 0)
    req = int(r.get("required_count") or 2)
    return cnt, req, (cnt >= req)

# ---------- ربط الصديق بالمُحيل ----------

def attach_referred_start(referrer_id: int, goal_token: str, referred_id: int) -> str:
    """
    تُستدعى عند /start ref-<referrer_id>-<token>
    تقوم بإنشاء سجل join بدون تحقق.
    """
    # نبحث عن الهدف عبر short_token للمُحيل:
    g = (get_table(GOALS_TBL)
         .select("*")
         .eq("referrer_id", int(referrer_id))
         .eq("short_token", goal_token)
         .in_("status", ["open","satisfied"])
         .order("created_at", desc=True)
         .limit(1)
         .execute())
    rows = getattr(g, "data", []) or []
    if not rows:
        return "⚠️ لم نجد مهمة خصم نشطة لهذا المُحيل."

    goal = rows[0]
    payload = {
        "goal_id": goal["id"],
        "referrer_id": int(referrer_id),
        "referred_id": int(referred_id),
        "start_payload": f"ref-{referrer_id}-{goal_token}",
    }
    try:
        # upsert-like: unique (referrer_id, referred_id)
        get_table(JOINS_TBL).insert(payload, upsert=True).execute()
    except Exception:
        # إذا فشل upsert، نتجاهل
        pass
    return "✅ تم ربطك بمُحيلك، اشترك في القناة ثم اضغط زر (تحققت)."

# ---------- تحقق الاشتراك وتفعيل الخصم ----------

def verify_and_count(bot, referrer_id: int, referred_id: int) -> Tuple[bool, str]:
    """
    يفحص اشتراك referred في القناة. إن كان مشتركًا:
    - يحدد verified_at, still_member=True
    - إن اكتمل العدد: ينشئ خصم user بنسبة 1% حتى تاريخ انتهاء الهدف.
    """
    # إيجاد آخر هدف مفتوح/قابل للتفعيل
    gq = (get_table(GOALS_TBL)
          .select("*")
          .eq("referrer_id", int(referrer_id))
          .in_("status", ["open","satisfied"])
          .order("created_at", desc=True)
          .limit(1)
          .execute())
    goals = getattr(gq, "data", []) or []
    if not goals:
        return False, "⚠️ لا يوجد هدف فعال لهذا اليوم."

    goal = goals[0]
    is_mem = _is_member(bot, referred_id)
    # حدّث سجل join
    try:
        upd = {
            "verified_at": _now().isoformat() if is_mem else None,
            "last_checked_at": _now().isoformat(),
            "still_member": bool(is_mem),
        }
        (get_table(JOINS_TBL)
         .update(upd)
         .eq("goal_id", goal["id"])
         .eq("referred_id", int(referred_id))
         .execute())
    except Exception as e:
        logging.warning(f"[referral] update join failed: {e}")

    if not is_mem:
        return False, "❌ لم يتم التحقق من اشتراكك في القناة بعد."

    # تحقق العدد
    cnt, req, sat = goal_progress(goal["id"])
    if sat and goal.get("granted_discount_id"):
        return True, "🎉 الخصم مفعّل مسبقاً لليوم."

    if sat:
        # أنشئ خصم user بنسبة 1% (ينتهي عند انتهاء الهدف)
        try:
            # مدة الخصم = الفرق الزمني الباقي حتى expires_at
            try:
                ends = datetime.fromisoformat(str(goal["expires_at"]).replace("Z","+00:00"))
            except Exception:
                ends = _now() + ONE_DAY
            # create 1% discount scope=user
            d = create_discount(scope="user", percent=1, user_id=int(referrer_id), active=True, days=None)
            did = (getattr(d, "data", []) or [{}])[0].get("id") if hasattr(d, "data") else d.get("id") if isinstance(d, dict) else None
            # نحدّث انتهاء الخصم ليطابق انتهاء الهدف
            if did:
                get_table(DISCOUNTS_TBL).update({"ends_at": ends.isoformat(), "meta": {"reason":"referral", "goal_id": goal["id"]}}).eq("id", did).execute()
            # اربط الهدف بالخصم
            get_table(GOALS_TBL).update({"status":"satisfied","granted_discount_id": did}).eq("id", goal["id"]).execute()
            return True, "🎉 تم تفعيل خصم 1% حتى نهاية اليوم. استمتع!"
        except Exception as e:
            logging.exception(f"[referral] create discount failed: {e}")
            return True, "✅ تم احتساب الصديق. سيُفعّل الخصم تلقائياً قريبًا."
    else:
        return True, f"✅ تم التحقق. التقدم {cnt}/{req}."

# ---------- إعادة تحقق لمنع الغش ----------

def revalidate_user_discount(bot, user_id: int) -> bool:
    """
    يُستدعى قبل الدفع: يعيد فحص اشتراك الأصدقاء المؤثّرين.
    إن لم يعد العدد مكتملاً: نعطّل خصم الإحالات (لا نعطّل خصمك الإداري).
    """
    gq = (get_table(GOALS_TBL)
          .select("*")
          .eq("referrer_id", int(user_id))
          .in_("status", ["open","satisfied"])
          .order("created_at", desc=True)
          .limit(1)
          .execute())
    goals = getattr(gq, "data", []) or []
    if not goals:
        return False

    goal = goals[0]
    # أعد فحص جميع المساهمين
    jq = (get_table(JOINS_TBL)
          .select("*")
          .eq("goal_id", goal["id"])
          .execute())
    joins = getattr(jq, "data", []) or []

    still = 0
    for j in joins:
        rid = int(j.get("referred_id"))
        is_mem = _is_member(bot, rid)
        try:
            (get_table(JOINS_TBL)
             .update({"still_member": bool(is_mem), "last_checked_at": _now().isoformat()})
             .eq("id", j["id"]).execute())
        except Exception:
            pass
        if is_mem:
            still += 1

    req = int(goal.get("required_count") or 2)
    ok = still >= req

    # فعّل/عطّل الخصم الممنوح لهذا الهدف فقط
    did = goal.get("granted_discount_id")
    if did:
        try:
            set_discount_active(did, ok)
        except Exception:
            # fallback: نهايته الآن
            if not ok:
                try:
                    get_table(DISCOUNTS_TBL).update({"active": False}).eq("id", did).execute()
                except Exception:
                    pass
    return ok

def expire_due_goals() -> None:
    """
    يُستدعى من المهمة المجدولة.
    """
    try:
        get_table("rpc").rpc("expire_old_referral_goals", {}).execute()
    except Exception:
        # أو نستدعي الـ function مباشرة
        try:
            get_table(GOALS_TBL).update({"status":"expired"}).lte("expires_at", _now().isoformat()).in_("status", ["open","satisfied"]).execute()
        except Exception:
            pass
            # أضف الكتلة التالية مباشرة بعد اكتمال الشرط:
from utils.time import now as _now
from services.discount_service import create_discount

REFERRAL_DISCOUNT_PERCENT = 1  # غيّر الرقم حسب نسبة خصم الإحالة لديك

# 1) أنشئ خصم المستخدم لمدة 14 ساعة من الآن
created = create_discount(
    scope="user",
    percent=REFERRAL_DISCOUNT_PERCENT,
    user_id=int(referrer_id),
    active=True,
    hours=14,
    source="referral",
    meta={"reason": "referral", "goal_id": str(goal["id"])}
)

# 2) اربط الخصم بالهدف للمراجعة، لكن لا تلمس خصم الأدمن
if created and created.get("id"):
    get_table("referral_goals").update({
        "status": "satisfied",
        "granted_discount_id": created["id"]
    }).eq("id", goal["id"]).execute()

