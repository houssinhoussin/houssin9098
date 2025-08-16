# handlers/quiz.py
# "🎯 الحزازير (ربحي)" بدون عدّاد.
# - عند الصح: نحذف رسالة السؤال فوراً ونرسل رسالة نجاح + زر ⏭️ التالي.
# - عند الخطأ: نرسل رسالة خطأ + زر 🔁 إعادة المحاولة و ⏸️ أكمل لاحقًا.
# - الجائزة في نهاية المرحلة تُضاف كنقاط (لا مال)، واللاعب يقرر التحويل لاحقاً.
# - تسجيل السؤال كـ seen في جدول quiz_seen عند العرض الأول.

from __future__ import annotations
import time
from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_attempt_price,
    reset_progress, next_question, deduct_fee_for_stage, add_points,
    user_quiz_state, convert_points_to_balance, load_template,
    compute_stage_reward_and_finalize, advance, get_points_value_syp,
    seen_mark,
)

# ------------------------ واجهة العرض ------------------------
def _question_text(stage_no: int, q_idx: int, item: dict) -> str:
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n\n"
        f"{item['text']}"
    )

def _question_markup(item: dict) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[
        types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}")
        for i, o in enumerate(item["options"])
    ])
    kb.add(types.InlineKeyboardButton(text="💳 تحويل النقاط إلى رصيد", callback_data="quiz_convert"))
    kb.add(types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_pause"))
    return kb

def _after_correct_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="⏭️ التالي (سيخصم عند العرض)", callback_data="quiz_next"))
    kb.add(types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_pause"))
    return kb

def _after_wrong_markup(price: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text=f"🔁 إعادة المحاولة (سيخصم {price} ل.س)", callback_data="quiz_retry"))
    kb.add(types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_pause"))
    return kb

def _intro_text(stage_no: int, price: int, total_q: int, bal: int, pts: int, syp_val: int) -> str:
    return (
        "ℹ️ <b>شرح سريع</b>\n"
        "• ٤ خيارات لكل سؤال.\n"
        "• تُخصم كلفة <b>المحاولة الأولى</b> عند الضغط على <i>التالي</i> أو <i>إعادة المحاولة</i>.\n"
        "• عند الخطأ يُعاد نفس السؤال (مع خصم جديد عند العرض).\n"
        "• لا تلميح للإجابة الصحيحة.\n\n"
        f"المرحلة: <b>{stage_no}</b> — الأسئلة: <b>{total_q}</b>\n"
        f"سعر المحاولة: <b>{price}</b> ل.س\n"
        f"💰 رصيدك: <b>{bal:,}</b> ل.س — 🏅 نقاطك: <b>{pts}</b> (≈ <b>{syp_val}</b> ل.س)"
    )

def _intro_markup(resume: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if resume:
        kb.add(types.InlineKeyboardButton(text="▶️ متابعة", callback_data="quiz_resume"))
    kb.add(types.InlineKeyboardButton(text="🚀 ابدأ الآن", callback_data="quiz_next"))
    kb.add(
        types.InlineKeyboardButton(text="🏅 نقاطي", callback_data="quiz_points"),
        types.InlineKeyboardButton(text="💳 تحويل النقاط", callback_data="quiz_convert"),
    )
    kb.add(types.InlineKeyboardButton(text="ℹ️ شرح اللعبة", callback_data="quiz_help"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

# ------------------------ شاشة تمهيد ------------------------
def _intro_screen(bot: TeleBot, chat_id: int, user_id: int, resume_only: bool = False):
    settings = load_settings()
    st = user_quiz_state.get(user_id, {}) or reset_progress(user_id)
    # مؤشرات المرحلة
    st.setdefault("stage_stars", 0)
    st.setdefault("stage_wrong_attempts", 0)
    st.setdefault("stage_done", 0)
    st.setdefault("attempts_on_current", 0)
    # تنظيف مراجع الرسائل السابقة
    st.pop("active_msg_id", None)
    st.pop("last_info_msg_id", None)
    st["last_click_ts"] = 0.0
    user_quiz_state[user_id] = st

    stage_no = int(st.get("stage", 1))
    tpl = load_template(st["template_id"])
    items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
    q_count = len(items)
    price   = get_attempt_price(stage_no, settings)

    bal, pts = get_wallet(user_id)
    syp_val  = get_points_value_syp(pts, settings)

    resume_avail = (int(st.get("q_index", 0)) > 0)

    txt = _intro_text(stage_no, price, q_count, bal, pts, syp_val)
    kb = _intro_markup(resume=(resume_avail and not resume_only))
    bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")

# ------------------------ نقطة الدخول ------------------------
def attach_handlers(bot: TeleBot):

    @bot.message_handler(func=lambda m: m.text == "🎯 الحزازير (ربحي)")
    def quiz_home(msg):
        user_id = msg.from_user.id
        name = (msg.from_user.first_name or "").strip()
        ensure_user_wallet(user_id, name)
        # بداية جديدة نظيفة
        st = reset_progress(user_id)
        st["stage_stars"] = 0
        st["stage_wrong_attempts"] = 0
        st["stage_done"] = 0
        st["attempts_on_current"] = 0
        st["last_click_ts"] = 0.0
        st.pop("active_msg_id", None)
        st.pop("last_info_msg_id", None)
        user_quiz_state[user_id] = st
        _intro_screen(bot, msg.chat.id, user_id)

    # --------------------------------------------
    # عرض السؤال (خصم مسبق) — يُستدعى من: quiz_next / quiz_retry / quiz_resume
    # يحاول حذف الرسالة السابقة (سؤال/نتيجة) قبل إرسال السؤال الجديد
    # --------------------------------------------
    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int, delete_msg_ids: list[int] | None = None) -> bool:
        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        # خصم السعر قبل الإظهار
        ok, new_bal, price = deduct_fee_for_stage(user_id, stage_no)
        if not ok:
            bal, _ = get_wallet(user_id)
            bot.send_message(
                chat_id,
                f"❌ رصيدك غير كافٍ لسعر المحاولة.\n"
                f"المطلوب: <b>{price}</b> ل.س — المتاح: <b>{bal}</b> ل.س",
                parse_mode="HTML"
            )
            return False

        # احذف أي رسائل قديمة (زر/نتيجة) قبل السؤال الجديد
        if delete_msg_ids:
            for mid in delete_msg_ids:
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass
        # احذف أيضًا السؤال النشط السابق إن وُجد
        old_q = st.get("active_msg_id")
        if old_q:
            try: bot.delete_message(chat_id, old_q)
            except Exception: pass

        # سجّل السؤال كـ "مُشاهَد" لمنع تكراره مستقبلًا
        try:
            tpl_id = st.get("template_id") or "T01"
            qid = str(item.get("id", f"{tpl_id}-{stage_no}-{q_idx}"))
            seen_mark(user_id, tpl_id, qid)
        except Exception:
            pass

        # أرسل السؤال الجديد
        txt = _question_text(stage_no, q_idx, item)
        sent = bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=_question_markup(item))

        # حدّث الحالة
        st["active_msg_id"] = sent.message_id
        st["started_at"]    = int(time.time() * 1000)
        user_quiz_state[user_id] = st
        return True

    # Debounce للنقرات (1s)
    def _click_guard(user_id: int) -> bool:
        st = user_quiz_state.get(user_id, {}) or {}
        now = time.time()
        last = float(st.get("last_click_ts", 0.0))
        if now - last < 1.0:
            return True
        st["last_click_ts"] = now
        user_quiz_state[user_id] = st
        return False

    # --------------------------------------------
    # أزرار التقدّم/الإيقاف
    # --------------------------------------------
    @bot.callback_query_handler(func=lambda c: c.data in ("quiz_next", "quiz_retry", "quiz_resume"))
    def on_next_or_retry(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass

        if _click_guard(user_id):
            return

        delete_ids = [call.message.message_id]  # احذف الرسالة التي ضغط منها
        _send_next_question(bot, chat_id, user_id, delete_msg_ids=delete_ids)

    # اختيار جواب
    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass

        if _click_guard(user_id):
            return

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)
        idx = int(call.data.split(":")[1])
        is_correct = (idx == int(item["correct_index"]))
        attempts_on_current = int(st.get("attempts_on_current", 0))

        # احذف رسالة السؤال القديمة فورًا
        active_mid = st.get("active_msg_id")
        if active_mid:
            try:
                bot.delete_message(chat_id, active_mid)
            except Exception:
                try: bot.edit_message_reply_markup(chat_id, active_mid, reply_markup=None)
                except Exception: pass

        if is_correct:
            # نجوم/نقاط
            stars_here = max(0, 3 - attempts_on_current)
            pmap = settings.get("points_per_stars", {"3": 3, "2": 2, "1": 1, "0": 0})
            award_pts = int(pmap.get(str(stars_here), stars_here))
            _, pts = add_points(user_id, award_pts)

            st["stage_stars"] = int(st.get("stage_stars", 0)) + stars_here
            st["stage_done"]  = int(st.get("stage_done", 0)) + 1
            st["attempts_on_current"] = 0
            user_quiz_state[user_id] = st

            # نهاية المرحلة؟
            tpl = load_template(st["template_id"])
            items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
            is_last_in_stage = (q_idx == len(items) - 1)

            ok_msg = bot.send_message(
                chat_id,
                f"✅ إجابة صحيحة! (+{award_pts} نقاط)\n"
                f"🏅 نقاطك الآن: <b>{pts}</b>",
                parse_mode="HTML",
                reply_markup=_after_correct_markup()
            )
            st["last_info_msg_id"] = ok_msg.message_id
            user_quiz_state[user_id] = st

            # جهّز التالي (ننتظر زر ⏭️ لخصم المحاولة التالية)
            advance(user_id)

            if is_last_in_stage:
                summary = compute_stage_reward_and_finalize(user_id, stage_no, len(items))
                bot.send_message(
                    chat_id,
                    "🏁 <b>ملخص المرحلة</b>\n"
                    f"المرحلة: <b>{stage_no}</b>\n"
                    f"الأسئلة المنجزة: <b>{summary['questions']}</b>\n"
                    f"المحاولات الخاطئة: <b>{summary['wrong_attempts']}</b>\n"
                    f"النجوم: <b>{summary['stars']}</b>\n"
                    f"🎁 الجائزة: <b>{summary['reward_pts']}</b> نقطة\n"
                    f"🏅 نقاطك الآن: <b>{summary['points_after']}</b>",
                    parse_mode="HTML"
                )

        else:
            # خطأ → زيادة عداد المحاولات
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            st["attempts_on_current"]  = attempts_on_current + 1
            user_quiz_state[user_id] = st

            price = get_attempt_price(stage_no, settings)
            wrong_msg = bot.send_message(
                chat_id,
                f"❌ إجابة خاطئة.\n"
                f"اضغط «إعادة المحاولة» لإعادة نفس السؤال (سيخصم <b>{price}</b> ل.س).",
                parse_mode="HTML",
                reply_markup=_after_wrong_markup(price)
            )
            st["last_info_msg_id"] = wrong_msg.message_id
            user_quiz_state[user_id] = st

    # تحويل النقاط إلى رصيد
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_convert")
    def on_convert(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass
        pts_before, syp_added, pts_after = convert_points_to_balance(user_id)
        if syp_added <= 0:
            try: bot.answer_callback_query(call.id, "لا توجد نقاط كافية للتحويل.", show_alert=True)
            except: pass
            return
        bot.send_message(
            chat_id,
            f"💳 تم تحويل <b>{pts_before}</b> نقطة إلى <b>{syp_added}</b> ل.س.\n"
            f"نقاطك الآن: <b>{pts_after}</b>.",
            parse_mode="HTML"
        )

    # عرض النقاط + الرصيد
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_points")
    def on_points(call):
        user_id = call.from_user.id
        settings = load_settings()
        bal, pts = get_wallet(user_id)
        syp_val  = get_points_value_syp(pts, settings)
        try:
            bot.answer_callback_query(call.id, f"الرصيد: {bal:,} ل.س — نقاطك: {pts} (≈ {syp_val} ل.س)", show_alert=False)
        except:
            pass

    # شرح اللعبة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_help")
    def on_help(call):
        try: bot.answer_callback_query(call.id)
        except: pass
        settings = load_settings()
        price_hint = get_attempt_price(1, settings)
        msg = (
            "ℹ️ <b>شرح اللعبة</b>\n"
            "• لديك ٤ خيارات لكل سؤال.\n"
            "• عند ضغط «ابدأ الآن/التالي» يُخصم ثمن <b>المحاولة الأولى</b> فورًا.\n"
            "• الإجابة الخاطئة = خصم جديد عند «إعادة المحاولة» لنفس السؤال.\n"
            "• لا نعرض أي تلميح للإجابة الصحيحة.\n"
            f"• مثال السعر (مرحلة 1): {price_hint} ل.س/محاولة (يتغيّر حسب المرحلة)."
        )
        bot.send_message(call.message.chat.id, msg, parse_mode="HTML")

    # إيقاف مؤقت (يرجع لشاشة التمهيد)
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_pause")
    def on_pause(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id, "تم الحفظ. رجعناك لبداية الزر.")
        except: pass

        st = user_quiz_state.get(user_id, {}) or {}
        last_info = st.get("last_info_msg_id")
        if last_info:
            try: bot.delete_message(chat_id, last_info)
            except Exception: pass
            st.pop("last_info_msg_id", None)

        user_quiz_state[user_id] = st
        _intro_screen(bot, chat_id, user_id, resume_only=False)

    # إلغاء
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        try: bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
