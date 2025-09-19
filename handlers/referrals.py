# handlers/referrals.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from telebot import types
from config import BOT_USERNAME, FORCE_SUB_CHANNEL_USERNAME
from services.referral_service import (
    get_or_create_today_goal,
    attach_referred_start,
    verify_and_count,
)
from services.referral_service import revalidate_user_discount

BTN_ADD_DISCOUNT = "➕ إضافة خصم"
BTN_CHECKED = "✅ تحققت"
BTN_REFRESH = "🔁 تحديث التقدم"
BTN_BACK = "⬅️ رجوع"

def _make_share_text(referrer_id: int, goal_token: str) -> tuple[str, str]:
    link = f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref-{referrer_id}-{goal_token}"
    text = (
        "🎁 *خصم 1% اليوم!* \n"
        "ادعُ صديقين للاشتراك بالقناة لتحصل على الخصم.\n"
        f"رابطك الشخصي:\n{link}"
    )
    return link, text

def _progress_text(g) -> str:
    req = int(g.get("required_count") or 2)
    left = g.get("expires_at")
    return (f"🎯 هدف اليوم: {req} أصدقاء\n"
            f"⏳ ينتهي: {left}\n"
            f"📌 القناة: {FORCE_SUB_CHANNEL_USERNAME}")

def register(bot, history):

    # زر القائمة الرئيسية
    @bot.message_handler(func=lambda m: m.text == BTN_ADD_DISCOUNT)
    def open_referral(msg):
        g = get_or_create_today_goal(msg.from_user.id, required_count=2)
        link, share = _make_share_text(msg.from_user.id, g["short_token"])
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("🔗 انسخ رابط دعوتك", switch_inline_query=share),
            types.InlineKeyboardButton("📢 اشترك بالقناة", url=f"https://t.me/{FORCE_SUB_CHANNEL_USERNAME.lstrip('@')}"),
            types.InlineKeyboardButton(BTN_REFRESH, callback_data="ref:refresh"),
            types.InlineKeyboardButton(BTN_BACK, callback_data="ref:back")
        )
        bot.send_message(
            msg.chat.id,
            f"{_progress_text(g)}\n\n*شارك الرابط مع صديقين ثم اطلب منهم الضغط على* {BTN_CHECKED}",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    # /start ref-<referrer_id>-<token>
    @bot.message_handler(commands=['start'])
    def start_with_ref(msg):
        parts = (msg.text or "").split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref-"):
            try:
                _, ref_uid, token = parts[1].split("-", 2)
                ref_uid = int(ref_uid)
            except Exception:
                return
            bot.send_message(
                msg.chat.id,
                f"أهلًا! اضغط زر ({BTN_CHECKED}) بعد الاشتراك في القناة {FORCE_SUB_CHANNEL_USERNAME}.",
                reply_markup=_sub_inline_kb()
            )
            # سجّل الربط
            bot.send_chat_action(msg.chat.id, "typing")
            attach_referred_start(ref_uid, token, msg.from_user.id)

    # زر "تحققت" للصديق
    def _sub_inline_kb():
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🔔 اشترك الآن في القناة", url=f"https://t.me/{FORCE_SUB_CHANNEL_USERNAME.lstrip('@')}"))
        kb.add(types.InlineKeyboardButton(BTN_CHECKED, callback_data="ref:checked"))
        return kb

    @bot.callback_query_handler(func=lambda c: c.data == "ref:checked")
    def cb_checked(c):
        # referrer = آخر مُحيل تعاملنا معه من الـ payload المخزون
        # للتبسيط: نطلب منه لصق رابطك الذي وصلك أو نقرأ من آخر attach (تم إنشاؤه في attach_referred_start)
        # سنحاول استنباط referrer من آخر goal مفتوح له نفس يوم اليوم
        # (تبسيط كافٍ لأن attach يسجِّل الزوج).
        # نحتاج referrer_id من الpayload؟ ليس متوفر هنا دائماً، لذا نعتمد على أحدث join لنفس referred.
        try:
            q = (get_table("referral_joins")
                 .select("*")
                 .eq("referred_id", c.from_user.id)
                 .order("first_seen_at", desc=True)
                 .limit(1)
                 .execute())
            row = (getattr(q, "data", []) or [None])[0]
            if not row:
                bot.answer_callback_query(c.id, "لا يوجد مُحيل مرتبط. استخدم رابط الدعوة.")
                return
            ok, msg = verify_and_count(bot, row["referrer_id"], c.from_user.id)
            bot.answer_callback_query(c.id, msg, show_alert=True)
        except Exception as e:
            bot.answer_callback_query(c.id, "حدث خطأ مؤقت. حاول ثانية.", show_alert=True)

    @bot.callback_query_handler(func=lambda c: c.data == "ref:refresh")
    def cb_refresh(c):
        try:
            # أعد التحقق من صحة الخصم قبل الشراء
            ok = revalidate_user_discount(bot, c.from_user.id)
            bot.answer_callback_query(c.id, "تم التحديث." if ok else "التقدم غير مكتمل.", show_alert=False)
        except Exception:
            bot.answer_callback_query(c.id, "تعذر التحديث الآن.", show_alert=False)

    @bot.callback_query_handler(func=lambda c: c.data == "ref:back")
    def cb_back(c):
        from handlers import keyboards
        bot.send_message(c.message.chat.id, "القائمة الرئيسية:", reply_markup=keyboards.main_menu())
