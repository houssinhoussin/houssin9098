# handlers/quiz.py
# "🎯 الحزازير (ربحي)" مع عدّاد يتحرك بتحرير نفس الرسالة (شاشة واحدة تتبدل)

from __future__ import annotations
import time
import threading
import random

from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_points_value_syp, get_attempt_price,
    reset_progress, next_question, add_points, load_template,
    user_quiz_state, ensure_paid_before_show, register_wrong_attempt, register_correct_answer,
    compute_stage_reward_and_finalize, set_runtime, get_runtime, clear_runtime, pick_template_for_user, persist_state,
    get_stage_time, convert_points_to_balance
)


# ---------- رسومات المُلخصات والرسائل ----------
def _pick_banter(group_key: str, stage_no: int, settings: dict) -> str:
    table = settings.get(group_key, {})
    acc = []
    for rng, msgs in table.items():
        try:
            lo, hi = [int(x) for x in rng.split("-")]
        except Exception:
            continue
        if lo <= stage_no <= hi and isinstance(msgs, list):
            acc.extend(msgs)
    return random.choice(acc) if acc else ""

def _windows_error(price: int, settings: dict) -> str:
    tpl = settings.get("windows_error_template") or (
        "🪟 <b>خطأ - Windows</b>\n"
        "<b>الرمز:</b> WRONG_ANSWER\n"
        "<b>الوصف:</b> الخيار غير صحيح أو انتهى الوقت.\n"
        "<b>الإجراء:</b> اضغط «إعادة المحاولة» (سيُخصم {price} ل.س)."
    )
    return tpl.replace("{price}", str(price))

def _windows_success(award_pts: int, total_pts: int, settings: dict) -> str:
    tpl = settings.get("windows_success_template") or (
        "🪟 <b>Windows - تهانينا</b>\n"
        "<b>الحدث:</b> CORRECT_ANSWER\n"
        "<b>الوصف:</b> إجابة صحيحة! (+{award_pts} نقاط)\n"
        "<b>إجمالي نقاطك:</b> {total_pts}\n"
        "<b>الإجراء:</b> استعد للسؤال التالي 🚀"
    )
    return (tpl
            .replace("{award_pts}", str(award_pts))
            .replace("{total_pts}", str(total_pts)))

def _question_text(item: dict, stage_no: int, q_idx: int, seconds_left: int, settings: dict, bal_hint: int | None) -> str:
    bar = settings.get("timer_bar_full", "🟩")  # مجرد رمز بسيط
    bal_line = f"\n💰 رصيدك: <b>{bal_hint:,}</b> ل.س" if bal_hint is not None else ""
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
        f"⏱️ {seconds_left:02d}s {bar}{bal_line}\n\n"
        f"{item['text']}"
    )

def _question_markup(item: dict) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[
        types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}")
        for i, o in enumerate(item["options"])
    ])
    return kb

def _edit_or_send(bot: TeleBot, chat_id: int, st: dict, text: str, markup: types.InlineKeyboardMarkup) -> int:
    """
    نحرّر نفس الرسالة إن وُجدت؛ وإلا نرسل رسالة جديدة. نعيد message_id الفعّال.
    """
    msg_id = st.get("active_msg_id")
    try:
        if msg_id:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return int(msg_id)
        else:
            m = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
            return int(m.message_id)
    except Exception:
        # في حال فشل التحرير (حُذفت الرسالة/تعارض)، نرسل جديدة
        m = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        return int(m.message_id)

def _intro_markup(resume: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if resume:
        kb.add(types.InlineKeyboardButton(text="▶️ متابعة", callback_data="quiz_resume"))
    kb.add(types.InlineKeyboardButton(text="🚀 ابدأ الآن", callback_data="quiz_next"))
    kb.add(
        types.InlineKeyboardButton(text="🏅 نقاطي", callback_data="quiz_points"),
        types.InlineKeyboardButton(text="💳 تحويل النقاط", callback_data="quiz_convert"),
    )
    # زر الترتيب حسب التقدّم
    kb.add(types.InlineKeyboardButton(text="🏆 الترتيب", callback_data="quiz_rank"))
    kb.add(types.InlineKeyboardButton(text="ℹ️ شرح اللعبة", callback_data="quiz_help"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

# رسائل ثابتة مختصرة (موجودة داخل settings.json أيضًا إن أردت تخصيصها)
def _help_text(settings: dict) -> str:
    return settings.get("help_text") or (
        "اللعبة أسئلة متعددة الخيارات.\n"
        "يتم خصم سعر قبل كل سؤال مرة واحدة.\n"
        "تحصل على نقاط عند إنهاء المرحلة، ويمكن تحويل النقاط إلى رصيد."
    )

# -------- شاشة تمهيد ------------------------
def _intro_screen(bot: TeleBot, chat_id: int, user_id: int, resume_only: bool = False):
    settings = load_settings()
    st = user_quiz_state.get(user_id, {}) or reset_progress(user_id)
    st.setdefault("stage_stars", 0)
    st.setdefault("stage_wrong_attempts", 0)
    st.setdefault("stage_done", 0)
    st.setdefault("attempts_on_current", 0)
    st.pop("active_msg_id", None)
    st.pop("last_info_msg_id", None)
    st["last_click_ts"] = 0.0
    user_quiz_state[user_id] = st
    persist_state(user_id)  # حفظ فوري

    stage_no = int(st.get("stage", 1))
    tpl = load_template(st["template_id"])
    items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
    q_count = len(items)
    price   = get_attempt_price(stage_no, settings)

    bal, pts = get_wallet(user_id)
    text = (
        "🎮 <b>مرحبًا!</b>\n\n"
        f"القالب: <b>{st['template_id']}</b>\n"
        f"المرحلة الحالية: <b>{stage_no}</b>\n"
        f"عدد أسئلة المرحلة: <b>{q_count}</b>\n"
        f"سعر المحاولة: <b>{price}</b> ل.س\n"
        f"رصيدك: <b>{bal}</b> ل.س — نقاطك: <b>{pts}</b>\n"
    )
    kb = _intro_markup(resume=bool(q_count and st.get("q_index", 0) < q_count))
    m = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
    st["active_msg_id"] = m.message_id
    user_quiz_state[user_id] = st
    persist_state(user_id)

# --------- بقية الهاندلرز الأساسية ----------
def wire_handlers(bot: TeleBot):

    # بدء
    @bot.message_handler(func=lambda m: True, content_types=['text'])
    def _catch_all(m):
        txt = (m.text or "").strip()

        # صيغ متعددة لتشغيل اللعبة
        QUIZ_TRIGGERS = {
            "/quiz",
            "🎯 الحزازير (ربحي)",
            "🎯 الحزازير",
            "الحزازير (ربحي)",
            "الحزازير",
            "quiz",
        }

        if txt in QUIZ_TRIGGERS:
            chat_id = m.chat.id
            user_id = m.from_user.id
            ensure_user_wallet(user_id, name=(m.from_user.first_name or "").strip())
            _intro_screen(bot, chat_id, user_id)
            return

        # ... باقي الراوترات النصية عندك هنا إن لزم ...

    # نقاطي
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_points")
    def on_points(call):
        user_id = call.from_user.id
        bal, pts = get_wallet(user_id)
        syp_val = get_points_value_syp(pts)
        try: bot.answer_callback_query(call.id)
        except: pass
        bot.send_message(call.message.chat.id, f"🏅 نقاطك: <b>{pts}</b> (≈ {syp_val} ل.س)\n💰 رصيدك: <b>{bal}</b> ل.س", parse_mode="HTML")

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
            f"💳 تم تحويل <b>{pts_before - pts_after}</b> نقطة إلى <b>{syp_added}</b> ل.س.\n"
            f"نقاطك الآن: <b>{pts_after}</b>.",
            parse_mode="HTML"
        )

    # لوحة الترتيب حسب التقدّم
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_rank")
    def on_rank(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass
        from services.quiz_service import get_leaderboard_by_progress
        top = get_leaderboard_by_progress(10)
        if not top:
            bot.send_message(chat_id, "لا توجد بيانات ترتيب بعد.", parse_mode="HTML")
            return
        lines = ["🏆 <b>الترتيب حسب التقدّم</b>"]
        for i, row in enumerate(top, start=1):
            nm = row.get("name") or f"UID{row.get('user_id')}"
            stg = row.get("stage", 0)
            done = row.get("stage_done", 0)
            lines.append(f"{i}. <b>{nm}</b> — مرحلة <b>{stg}</b>، منجز <b>{done}</b> سؤالًا")
        bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

    # شرح
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_help")
    def on_help(call):
        try: bot.answer_callback_query(call.id)
        except: pass
        bot.send_message(call.message.chat.id, _help_text(load_settings()), parse_mode="HTML")

    # التالي/متابعة: شاشة واحدة + عداد متحرك
    @bot.callback_query_handler(func=lambda c: c.data in ("quiz_next", "quiz_resume"))
    def on_next(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        # احرص على وجود المحفظة
        ensure_user_wallet(user_id)

        # أوقف أي مؤقّت سابق قبل البدء
        rt_prev = get_runtime(user_id)
        cancel_prev = rt_prev.get("timer_cancel")
        if cancel_prev:
            try: cancel_prev.set()
            except: pass

        # جرّب الخصم لمرة واحدة قبل عرض السؤال
        ok, bal_or_new, price, reason = ensure_paid_before_show(user_id)
        if not ok:
            try: bot.answer_callback_query(call.id, "رصيدك غير كافٍ لهذه المحاولة.", show_alert=True)
            except: pass
            return

        st, item, stage_no, q_idx = next_question(user_id)
        settings = load_settings()

        # وقت المرحلة
        seconds = get_stage_time(stage_no, settings)

        # عرض/تحرير السؤال في نفس الرسالة
        kb = _question_markup(item)
        txt = _question_text(item, stage_no, q_idx, seconds, settings, bal_or_new if reason == "paid" else None)
        msg_id = _edit_or_send(bot, chat_id, st, txt, kb)

        st["active_msg_id"] = msg_id
        st["started_at"] = time.time()
        st["attempts_on_current"] = 0
        user_quiz_state[user_id] = st
        persist_state(user_id)

        # مؤقّت الخلفية: يعدّل نفس الرسالة كل tick
        cancel = threading.Event()
        set_runtime(user_id, timer_cancel=cancel)
        tick = max(1, int(settings.get("timer_tick_seconds", 5)))  # اضبطها 5 في settings.json لو تريد كل خمس ثوانٍ

        def _timer():
            remain = seconds
            # تحديث دوري
            while remain > 0 and not cancel.is_set():
                time.sleep(tick)
                remain -= tick
                if cancel.is_set():
                    return
                try:
                    new_txt = _question_text(item, stage_no, q_idx, max(0, remain), settings,
                                             bal_or_new if reason == "paid" else None)
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=new_txt,
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                except Exception:
                    # تجاهل أخطاء التحرير (الرسالة لم تتغير / تضارب تحرير)
                    pass

            if cancel.is_set():
                return

            # انتهى الوقت ⇒ خطأ (تحرير نفس الرسالة)
            register_wrong_attempt(user_id)
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_windows_error(get_attempt_price(stage_no, settings), settings),
                    parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="🔁 إعادة المحاولة", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass

        threading.Thread(target=_timer, daemon=True).start()

    # اختيار الإجابة (تحرير نفس الرسالة وإيقاف المؤقّت)
    @bot.callback_query_handler(func=lambda c: c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass

        # أوقف المؤقّت الجاري (إن وُجد)
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass

        st, item, stage_no, q_idx = next_question(user_id)
        msg_id = st.get("active_msg_id")

        try:
            chosen = int(call.data.split(":", 1)[1])
        except Exception:
            chosen = -1

        if chosen != int(item.get("correct_index", -1)):
            # خطأ ⇒ تحرير نفس الرسالة
            register_wrong_attempt(user_id)
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_windows_error(get_attempt_price(stage_no, load_settings()), load_settings()),
                    parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="🔁 إعادة المحاولة", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass
            return

        # صح
        register_correct_answer(user_id)

        # تحيين التقدّم
        tpl = load_template(st["template_id"])
        total_q = len(tpl.get("items_by_stage", {}).get(str(stage_no), []) or [])
        st["attempts_on_current"] = int(st.get("attempts_on_current", 0)) + 1
        st["q_index"] = int(st.get("q_index", 0)) + 1
        user_quiz_state[user_id] = st
        persist_state(user_id)

        if st["q_index"] >= total_q:
            # أنهى المرحلة ⇒ تحرير نفس الرسالة برسالة النجاح
            result = compute_stage_reward_and_finalize(user_id, stage_no, total_q)
            _, pts_now = get_wallet(user_id)
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_windows_success(result.get("reward_points", 0), pts_now, load_settings()),
                    parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="⏭️ التالي", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass
        else:
            # سؤال تالي بنفس المرحلة ⇒ تحرير نفس الرسالة + مؤقّت جديد
            settings = load_settings()
            st2, item2, stage_no2, q_idx2 = next_question(user_id)
            seconds2 = get_stage_time(stage_no2, settings)
            kb2 = _question_markup(item2)
            txt2 = _question_text(item2, stage_no2, q_idx2, seconds2, settings, None)

            new_msg_id = _edit_or_send(bot, chat_id, st2, txt2, kb2)

            st2["active_msg_id"] = new_msg_id
            st2["started_at"] = time.time()
            st2["attempts_on_current"] = 0
            user_quiz_state[user_id] = st2
            persist_state(user_id)

            # مؤقّت جديد للسؤال التالي
            cancel2 = threading.Event()
            set_runtime(user_id, timer_cancel=cancel2)
            tick = max(1, int(settings.get("timer_tick_seconds", 5)))

            def _timer2():
                remain = seconds2
                while remain > 0 and not cancel2.is_set():
                    time.sleep(tick)
                    remain -= tick
                    if cancel2.is_set():
                        return
                    try:
                        new_txt = _question_text(item2, stage_no2, q_idx2, max(0, remain), settings, None)
                        bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=new_msg_id,
                            text=new_txt,
                            parse_mode="HTML",
                            reply_markup=kb2
                        )
                    except Exception:
                        pass
                if cancel2.is_set():
                    return
                register_wrong_attempt(user_id)
                try:
                    bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=new_msg_id,
                        text=_windows_error(get_attempt_price(stage_no2, settings), settings),
                        parse_mode="HTML",
                        reply_markup=types.InlineKeyboardMarkup().add(
                            types.InlineKeyboardButton(text="🔁 إعادة المحاولة", callback_data="quiz_next"),
                            types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                        )
                    )
                except Exception:
                    pass

            threading.Thread(target=_timer2, daemon=True).start()

    # إلغاء
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        user_id = call.from_user.id
        try: bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass
        clear_runtime(user_id)
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass


# توافق مع الاستدعاء في main.py
attach_handlers = wire_handlers
