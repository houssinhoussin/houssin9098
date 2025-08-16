# handlers/quiz.py
# زر "🎯 الحزازير (ربحي)" + شاشة تمهيد + عدّاد 60s + استئناف + شرح اللعبة + عرض الرصيد والنقاط
# + مزاح لبناني/سوري + تنبيه خطأ/نجاح ستايل Windows
from __future__ import annotations
import threading
import time
import random
from typing import Optional

from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_attempt_price,
    reset_progress, next_question, deduct_fee_for_stage, add_points,
    user_quiz_state,                # حالة دائمة قابلة للتسلسل
    convert_points_to_balance,
    get_runtime, set_runtime, clear_runtime,   # حالة وقتية (RAM)
    load_template,
    compute_stage_reward_and_finalize,
    get_points_value_syp,
)

# ------------------------ أدوات واجهة ------------------------
def _timer_bar(total: int, left: int, full: str, empty: str) -> str:
    slots = 12  # 60ث / 5ث = 12 خطوات
    filled = max(0, min(slots, round((left/total)*slots)))
    return full * filled + empty * (slots - filled)

def _question_text(stage_no: int, q_idx: int, item: dict, settings: dict, seconds_left: int, bal_hint: int | None = None) -> str:
    bar = _timer_bar(settings["seconds_per_question"], seconds_left, settings["timer_bar_full"], settings["timer_bar_empty"])
    bal_line = f"\n💰 رصيدك: <b>{bal_hint:,}</b> ل.س" if bal_hint is not None else ""
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
        f"⏳ {seconds_left}s {bar}{bal_line}\n\n"
        f"{item['text']}"
    )

def _options_markup(item: dict, bal: int | None = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}") for i, o in enumerate(item["options"])]
    kb.add(*buttons)
    if bal is not None:
        kb.add(types.InlineKeyboardButton(text=f"💰 الرصيد: {bal:,} ل.س", callback_data="quiz_balance"))
    kb.add(
        types.InlineKeyboardButton(text="🏅 نقاطي", callback_data="quiz_points"),
        types.InlineKeyboardButton(text="💳 تحويل النقاط", callback_data="quiz_convert"),
    )
    kb.add(types.InlineKeyboardButton(text="ℹ️ شرح اللعبة", callback_data="quiz_help"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

def _retry_markup(price: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(text=f"🔁 إعادة المحاولة (سيُخصم {price} ل.س)", callback_data="quiz_retry"))
    kb.add(types.InlineKeyboardButton(text="❌ إنهاء", callback_data="quiz_exit"))
    kb.add(types.InlineKeyboardButton(text="ℹ️ شرح اللعبة", callback_data="quiz_help"))
    return kb

def _intro_markup(resume: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if resume:
        kb.add(types.InlineKeyboardButton(text="▶️ متابعة", callback_data="quiz_resume"))
    kb.add(types.InlineKeyboardButton(text="🚀 ابدأ الآن", callback_data="quiz_start_stage"))
    kb.add(types.InlineKeyboardButton(text="🏅 نقاطي", callback_data="quiz_points"),
           types.InlineKeyboardButton(text="💳 تحويل النقاط", callback_data="quiz_convert"))
    kb.add(types.InlineKeyboardButton(text="ℹ️ شرح اللعبة", callback_data="quiz_help"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

# ------------------------ مزاح/تشجيع ------------------------
def _pick_banter(group_key: str, stage_no: int, settings: dict) -> str:
    table = settings.get(group_key, {})
    if not isinstance(table, dict):
        return ""
    candidates = []
    for rng, msgs in table.items():
        try:
            lo, hi = [int(x) for x in rng.split("-")]
        except Exception:
            continue
        if lo <= stage_no <= hi and isinstance(msgs, list) and msgs:
            candidates.extend(msgs)
    if not candidates:
        return ""
    return random.choice(candidates)

def _windows_error(price: int, settings: dict) -> str:
    tpl = settings.get("windows_error_template") or "🪟 <b>خطأ - Windows</b>\n<b>الرمز:</b> WRONG_ANSWER\n<b>الوصف:</b> الخيار غير صحيح.\n<b>الإجراء:</b> اضغط «إعادة المحاولة» (سيُخصم {price} ل.س)."
    return tpl.replace("{price}", str(price))

def _windows_success(award_pts: int, total_pts: int, settings: dict) -> str:
    tpl = settings.get("windows_success_template") or "🪟 <b>Windows - تهانينا</b>\n<b>الحدث:</b> CORRECT_ANSWER\n<b>الوصف:</b> إجابة صحيحة! (+{award_pts} نقاط)\n<b>إجمالي نقاطك:</b> {total_pts}\n<b>الإجراء:</b> استعد للسؤال التالي 🚀"
    return tpl.replace("{award_pts}", str(award_pts)).replace("{total_pts}", str(total_pts))

# ------------------------ مؤقّت التحديث (تحرير نفس الرسالة) ------------------------
def _start_timer(bot: TeleBot, chat_id: int, msg_id: int, user_id: int, settings: dict):
    total = int(settings["seconds_per_question"])
    tick  = 5  # تقليل التحرير (آمن مع تيليجرام)

    cancel = threading.Event()
    set_runtime(user_id, timer_cancel=cancel, last_answer_ts=0)

    def _loop():
        left = total
        while left > 0 and not cancel.is_set():
            try:
                st, item, stage_no, q_idx = next_question(user_id)
                bal_hint = int(st.get("last_balance", 0))
                txt = _question_text(stage_no, q_idx, item, settings, left, bal_hint=bal_hint)
                kb  = _options_markup(item, bal=bal_hint)
                bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            time.sleep(tick)
            left -= tick

        # ⌛ انتهى الوقت → محاولة خاطئة + تنبيه إعادة/إنهاء
        if not cancel.is_set():
            st = user_quiz_state.get(user_id, {})
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            st["attempts_on_current"]  = int(st.get("attempts_on_current", 0)) + 1
            user_quiz_state[user_id] = st

            try:
                _, item, stage_no, _ = next_question(user_id)
                price = get_attempt_price(stage_no, load_settings())
                banter = _pick_banter("banter_wrong_by_stage", stage_no, settings)
                txt = f"💬 {banter}\n{_windows_error(price, settings)}"
                bot.edit_message_text(txt, chat_id, msg_id, reply_markup=_retry_markup(price), parse_mode="HTML")
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()

# ------------------------ شاشة تمهيد/استئناف ------------------------
def _intro_screen(bot: TeleBot, chat_id: int, user_id: int, resume_only: bool = False):
    settings = load_settings()
    st = user_quiz_state.get(user_id, {})
    if not st:
        st = reset_progress(user_id)
        st["stage_stars"] = 0
        st["stage_wrong_attempts"] = 0
        st["stage_done"] = 0
        st["attempts_on_current"] = 0
        user_quiz_state[user_id] = st

    stage_no = int(st.get("stage", 1))
    tpl = load_template(st["template_id"])
    items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
    q_count = len(items)
    price   = get_attempt_price(stage_no, settings)

    bal, pts = get_wallet(user_id)
    syp_val  = get_points_value_syp(pts, settings)

    resume_avail = (int(st.get("q_index", 0)) > 0 or st.get("active_msg_id"))

    txt = (
        "🎯 <b>الحزازير (ربحي)</b>\n"
        f"المرحلة الحالية: <b>{stage_no}</b> — عدد الأسئلة: <b>{q_count}</b>\n"
        f"💸 سعر المحاولة: <b>{price}</b> ل.س\n"
        f"💰 رصيدك: <b>{bal:,}</b> ل.س — 🏅 نقاطك: <b>{pts}</b> (≈ <b>{syp_val}</b> ل.س)\n\n"
        "اضغط «🚀 ابدأ الآن» لخصم أول محاولة وعرض السؤال، أو «▶️ متابعة» إن كان لديك مرحلة قيد التقدم."
    )
    kb = _intro_markup(resume=(resume_avail and not resume_only))
    bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")

# ------------------------ نقطة دخول: زر القائمة ------------------------
def attach_handlers(bot: TeleBot):

    @bot.message_handler(func=lambda m: m.text == "🎯 الحزازير (ربحي)")
    def quiz_home(msg):
        user_id = msg.from_user.id
        name = (msg.from_user.first_name or "").strip()
        ensure_user_wallet(user_id, name)
        _intro_screen(bot, msg.chat.id, user_id)

    # بدء المرحلة (خصم أول محاولة ثم عرض السؤال)
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_start_stage")
    def start_stage(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

        st = user_quiz_state.get(user_id, {}) or reset_progress(user_id)
        st.setdefault("stage_stars", 0)
        st.setdefault("stage_wrong_attempts", 0)
        st.setdefault("stage_done", 0)
        st.setdefault("attempts_on_current", 0)
        user_quiz_state[user_id] = st

        _send_next_question(bot, chat_id, user_id)

    # استئناف المرحلة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_resume")
    def resume_stage(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()
        _send_next_question(bot, chat_id, user_id)

    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int):
        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        # خصم السعر قبل إظهار السؤال
        ok, new_bal, price = deduct_fee_for_stage(user_id, stage_no)
        if not ok:
            bal, _ = get_wallet(user_id)
            bot.send_message(
                chat_id,
                f"❌ رصيدك غير كافٍ لسعر السؤال.\n"
                f"السعر المطلوب: <b>{price}</b> ل.س\n"
                f"رصيدك المتاح: <b>{bal}</b> ل.س",
                parse_mode="HTML"
            )
            return

        st["last_balance"] = new_bal
        user_quiz_state[user_id] = st

        txt = _question_text(stage_no, q_idx, item, settings, settings["seconds_per_question"], bal_hint=new_bal)
        kb  = _options_markup(item, bal=new_bal)
        sent = bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")

        st["active_msg_id"] = sent.message_id
        st["started_at"]    = int(time.time() * 1000)
        user_quiz_state[user_id] = st

        _start_timer(bot, chat_id, sent.message_id, user_id, settings)

    # اختيار جواب
    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        # Debounce 1s
        rt = get_runtime(user_id)
        now = time.time()
        last = float(rt.get("last_answer_ts", 0))
        if now - last < 1.0:
            try: bot.answer_callback_query(call.id)
            except: pass
            return
        set_runtime(user_id, last_answer_ts=now)

        # أوقف المؤقت
        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)
        idx = int(call.data.split(":")[1])
        is_correct = (idx == int(item["correct_index"]))
        attempts_on_current = int(st.get("attempts_on_current", 0))

        if is_correct:
            # حساب النجوم والنقاط
            stars_here = max(0, 3 - attempts_on_current)
            pmap = settings.get("points_per_stars", {"3": 3, "2": 2, "1": 1, "0": 0})
            award_pts = int(pmap.get(str(stars_here), stars_here))
            _, pts = add_points(user_id, award_pts)

            st["stage_stars"] = int(st.get("stage_stars", 0)) + stars_here
            st["stage_done"]  = int(st.get("stage_done", 0)) + 1
            st["attempts_on_current"] = 0
            user_quiz_state[user_id] = st

            tpl = load_template(st["template_id"])
            items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
            is_last_in_stage = (q_idx == len(items) - 1)

            banter_ok = _pick_banter("banter_ok_by_stage", stage_no, settings)
            success_box = _windows_success(award_pts, pts, settings)
            txt = (
                f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
                f"{item['text']}\n\n"
                f"{success_box}\n"
                f"💬 {banter_ok}"
            )
            try:
                bot.edit_message_text(txt, chat_id, call.message.message_id, parse_mode="HTML")
            except Exception:
                pass

            def _after_ok():
                from services.quiz_service import advance
                advance(user_id)
                if is_last_in_stage:
                    summary = compute_stage_reward_and_finalize(user_id, stage_no, len(items))
                    msg = (
                        "🏁 <b>ملخص المرحلة</b>\n"
                        f"المرحلة: <b>{stage_no}</b>\n"
                        f"الأسئلة المنجزة: <b>{summary['questions']}</b>\n"
                        f"المحاولات الخاطئة: <b>{summary['wrong_attempts']}</b>\n"
                        f"النجوم: <b>{summary['stars']}</b>\n"
                        f"🎁 الجائزة: <b>{summary['reward_syp']}</b> ل.س\n"
                        f"💰 رصيدك الآن: <b>{summary['balance_after']}</b> ل.س"
                    )
                    bot.send_message(chat_id, msg, parse_mode="HTML")
                _send_next_question(bot, chat_id, user_id)

            threading.Timer(2.0, _after_ok).start()

        else:
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            st["attempts_on_current"]  = attempts_on_current + 1
            user_quiz_state[user_id] = st

            price = get_attempt_price(stage_no, settings)
            banter = _pick_banter("banter_wrong_by_stage", stage_no, settings)
            txt = (
                f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
                f"{item['text']}\n\n"
                f"💬 {banter}\n"
                f"{_windows_error(price, settings)}"
            )
            try:
                bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=_retry_markup(price), parse_mode="HTML")
            except Exception:
                pass

    # إعادة المحاولة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_retry")
    def on_retry(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()
        _send_next_question(bot, chat_id, user_id)

    # خروج
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_exit")
    def on_exit(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id, "تم الإنهاء.")
        except: pass
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()
        clear_runtime(user_id)
        _intro_screen(bot, chat_id, user_id, resume_only=False)

    # تحويل النقاط
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_convert")
    def on_convert(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        pts_before, syp_added, pts_after = convert_points_to_balance(user_id)
        if syp_added <= 0:
            try: bot.answer_callback_query(call.id, "لا توجد نقاط كافية للتحويل.", show_alert=True)
            except: pass
            return
        try: bot.answer_callback_query(call.id, "تم التحويل!", show_alert=False)
        except: pass
        bot.send_message(
            chat_id,
            f"💳 تم تحويل <b>{pts_before}</b> نقطة إلى <b>{syp_added}</b> ل.س.\n"
            f"نقاطك الآن: <b>{pts_after}</b>.",
            parse_mode="HTML"
        )

    # عرض النقاط
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_points")
    def on_points(call):
        user_id = call.from_user.id
        settings = load_settings()
        _, pts = get_wallet(user_id)
        syp_val  = get_points_value_syp(pts, settings)
        try:
            call.bot.answer_callback_query(call.id, f"نقاطك: {pts} ≈ {syp_val} ل.س", show_alert=False)
        except Exception:
            pass

    # عرض الرصيد
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_balance")
    def on_balance(call):
        user_id = call.from_user.id
        bal, _ = get_wallet(user_id)
        try:
            call.bot.answer_callback_query(call.id, f"رصيدك: {bal} ل.س", show_alert=False)
        except Exception:
            pass

    # شرح اللعبة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_help")
    def on_help(call):
        try: call.bot.answer_callback_query(call.id)
        except: pass
        settings = load_settings()
        secs = settings.get("seconds_per_question", 60)
        msg = (
            "ℹ️ <b>شرح اللعبة</b>\n"
            f"• لديك <b>{secs} ثانية</b> لكل سؤال.\n"
            "• عند ضغط «ابدأ الآن» يُخصم ثمن <b>المحاولة الأولى</b> فورًا.\n"
            "• الإجابة الخاطئة أو انتهاء الوقت = خصم جديد عند إعادة المحاولة.\n"
            "• لا توجد تلميحات؛ نعيد نفس السؤال بترتيب خيارات مُبدّل.\n"
            "• تحصل على نقاط حسب الأداء ويمكنك تحويلها إلى رصيد في أي وقت.\n"
            "• يمكنك المتابعة لاحقًا من حيث توقفت عبر نفس الزر."
        )
        call.message.bot.send_message(call.message.chat.id, msg, parse_mode="HTML")

    # إلغاء
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        user_id = call.from_user.id
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()
        clear_runtime(user_id)
        try: call.bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass
        try: call.message.bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
