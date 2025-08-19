# handlers/quiz.py
# "🎯 الحزازير (ربحي)" شاشة واحدة تتحرّك بالتحرير + رسائل احترافية للخطأ/النجاح

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
    get_stage_time, convert_points_to_balance, award_points_for_correct, wipe_user_for_fresh_start, get_progress
)

# ---------- رسومات/نصوص ----------

def _pick_banter(group_key: str, stage_no: int, settings: dict) -> str:
    table = settings.get(group_key, {})
    acc = []
    for rng, msgs in table.items():
        try:
            if rng.endswith("+"):
                lo = int(rng[:-1])
                ok = (stage_no >= lo)
            elif "-" in rng:
                lo, hi = [int(x) for x in rng.split("-", 1)]
                ok = (lo <= stage_no <= hi)
            else:
                ok = (int(rng) == stage_no)
        except Exception:
            ok = False
        if ok and isinstance(msgs, list):
            acc.extend(msgs)
    return random.choice(acc) if acc else ""

def _fmt_error(kind: str, price: int, settings: dict, banter: str | None, bal: int, pts: int) -> str:
    """
    قالب خطأ احترافي:
    kind in {"wrong","timeout"}
    يعرض الرصيد والنقاط دائمًا.
    """
    if kind == "timeout":
        body = ("❌ <b>انتهى الوقت</b>\n"
                "<b>التنبيه:</b> بالضغط على «إعادة المحاولة» سيتم خصم {price} ل.س.")
    else:
        body = ("❌ <b>إجابة خاطئة</b>\n"
                "<b>التنبيه:</b> بالضغط على «إعادة المحاولة» سيتم خصم {price} ل.س.")
    head = (banter + "\n\n") if banter else ""
    footer = f"\n\n💰 رصيدك: <b>{bal:,}</b> ل.س — 🏅 نقاطك: <b>{pts:,}</b>"
    return head + body.replace("{price}", str(price)) + footer

def _fmt_success_end(award_pts: int, total_pts: int, settings: dict, banter: str | None, bal: int) -> str:
    tpl = settings.get("windows_success_template") or (
        "✅ <b>تهانينا</b>\n"
        "<b>الحدث:</b> إتمام المرحلة\n"
        "<b>المكافأة:</b> +{award_pts} نقاط\n"
        "<b>إجمالي نقاطك:</b> {total_pts}\n"
        "<b>التالي:</b> اضغط «متابعة» للانتقال."
    )
    body = (tpl
            .replace("{award_pts}", str(award_pts))
            .replace("{total_pts}", str(total_pts)))
    footer = f"\n\n💰 رصيدك: <b>{bal:,}</b> ل.س — 🏅 نقاطك: <b>{total_pts:,}</b>"
    return (banter + "\n\n" + body if banter else body) + footer

def _fmt_success_mid(settings: dict, banter: str | None, delta_pts: int, bal: int, pts: int) -> str:
    # نص واضح: السؤال التالي بدون خصم
    head = "✅ <b>إجابة صحيحة</b>\n"
    banter_txt = (banter + "\n") if banter else ""
    info = f"🏅 +{delta_pts} نقاط (الإجمالي: <b>{pts:,}</b>) — 💰 رصيدك: <b>{bal:,}</b> ل.س\n"
    tail = "ℹ️ لن يتم الخصم في <b>السؤال التالي</b> إذا انتقلت الآن."
    return head + banter_txt + info + tail

def _timer_bar(remaining: int, full_seconds: int, settings: dict) -> str:
    full = settings.get("timer_bar_full", "🟩")
    empty = settings.get("timer_bar_empty", "⬜")
    full_seconds = max(1, int(full_seconds))
    total_slots = 10
    ratio = max(0.0, min(1.0, remaining / float(full_seconds)))
    filled = max(0, min(total_slots, int(round(ratio * total_slots))))
    return full * filled + empty * (total_slots - filled)

def _question_text(item: dict, stage_no: int, q_idx: int, seconds_left: int, full_seconds: int, settings: dict, show_charge_line: bool, bal_after_charge: int | None) -> str:
    """
    show_charge_line=True يعني تم الخصم الآن؛ نعرض معه سطر تنبيه الخصم + الرصيد.
    """
    bar = _timer_bar(seconds_left, full_seconds, settings)
    charge_line = ""
    bal_line = ""
    if show_charge_line and bal_after_charge is not None:
        price_now = get_attempt_price(stage_no, settings)
        charge_line = f"\n💸 تم خصم <b>{price_now:,}</b> ل.س لهذه المحاولة"
        bal_line = f"\n💰 رصيدك: <b>{bal_after_charge:,}</b> ل.س"
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
        f"⏱️ {seconds_left:02d}s {bar}{bal_line}{charge_line}\n\n"
        f"{item['text']}"
    )

def _question_markup(item: dict) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[
        types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}")
        for i, o in enumerate(item["options"])
    ])
    return kb

def _edit_or_send(bot: TeleBot, chat_id: int, st: dict, text: str, markup: types.InlineKeyboardMarkup | None) -> int:
    """
    يحاول التحرير للحفاظ على شاشة واحدة. عند الفشل يُرسل رسالة جديدة
    ويحذف الرسالة السابقة إن وجدت لضمان عدم تراكم الواجهة.
    """
    msg_id = st.get("active_msg_id")
    try:
        if msg_id:
            bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML", reply_markup=markup)
            return int(msg_id)
        else:
            m = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
            return int(m.message_id)
    except Exception:
        m = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        # حذف الواجهة السابقة إن وُجدت لضمان شاشة واحدة
        try:
            if msg_id:
                bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        return int(m.message_id)

def _intro_markup(can_resume: bool) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="🆕 ابدأ اللعب", callback_data="quiz_startover"))
    if can_resume:
        kb.add(types.InlineKeyboardButton(text="▶️ متابعة", callback_data="quiz_resume"))
    # أزرار ثانوية
    kb.add(
        types.InlineKeyboardButton(text="🏅 نقاطي", callback_data="quiz_points"),
        types.InlineKeyboardButton(text="💳 تحويل النقاط", callback_data="quiz_convert"),
    )
    kb.add(types.InlineKeyboardButton(text="🏆 الترتيب", callback_data="quiz_rank"))
    kb.add(types.InlineKeyboardButton(text="ℹ️ شرح اللعبة", callback_data="quiz_help"))
    # ملاحظة: لا نعرض زر "إلغاء" ضمن واجهة المقدّمة كي لا يعيدها لنفسها
    return kb

def _help_text(settings: dict) -> str:
    return settings.get("help_text") or (
        "اللعبة أسئلة متعددة الخيارات.\n"
        "يتم خصم سعر قبل كل سؤال مرة واحدة.\n"
        "تحصل على نقاط عند إنهاء المرحلة، ويمكن تحويل النقاط إلى رصيد."
    )

# -------- شاشة تمهيد ----------
def _intro_screen(bot: TeleBot, chat_id: int, user_id: int):
    settings = load_settings()
    st = user_quiz_state.get(user_id) or get_progress(user_id) or reset_progress(user_id)
    st.setdefault("stage_stars", 0)
    st.setdefault("stage_wrong_attempts", 0)
    st.setdefault("stage_done", 0)
    st.setdefault("attempts_on_current", 0)
    st["no_charge_next"] = 0  # بداية جديدة تلغي أي إعفاء سابق
    st.pop("last_info_msg_id", None)
    st["last_click_ts"] = 0.0
    user_quiz_state[user_id] = st
    persist_state(user_id)

    stage_no = int(st.get("stage", 1))
    tpl = load_template(st["template_id"])
    items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
    q_count = len(items)
    price   = get_attempt_price(stage_no, settings)

    bal, pts = get_wallet(user_id)
    can_resume = bool(q_count and (int(st.get("q_index", 0)) > 0 or int(st.get("stage_done", 0)) > 0))

    text = (
        "🎮 <b>مرحبًا!</b>\n\n"
        f"القالب: <b>{st['template_id']}</b>\n"
        f"المرحلة الحالية: <b>{stage_no}</b>\n"
        f"عدد أسئلة المرحلة: <b>{q_count}</b>\n"
        f"سعر المحاولة: <b>{price}</b> ل.س\n"
        f"💰 رصيدك: <b>{bal}</b> ل.س — 🏅 نقاطك: <b>{pts}</b>\n"
        "اختر: <b>🆕 ابدأ اللعب</b> لبداية جديدة (تصفيير النقاط والتقدّم)، أو <b>▶️ متابعة</b> إن كان لديك تقدّم."
    )
    kb = _intro_markup(can_resume=can_resume)

    msg_id = _edit_or_send(bot, chat_id, st, text, kb)
    st["active_msg_id"] = msg_id
    user_quiz_state[user_id] = st
    persist_state(user_id)

# -------- هاندلرز ----------
def wire_handlers(bot: TeleBot):

    # بدء
    @bot.message_handler(func=lambda m: True, content_types=['text'])
    def _catch_all(m):
        txt = (m.text or "").strip()
        QUIZ_TRIGGERS = {"/quiz", "🎯 الحزازير (ربحي)", "🎯 الحزازير", "الحزازير (ربحي)", "الحزازير", "quiz"}
        if txt in QUIZ_TRIGGERS:
            chat_id = m.chat.id
            user_id = m.from_user.id
            ensure_user_wallet(user_id, name=(m.from_user.first_name or "").strip())
            _intro_screen(bot, chat_id, user_id)
            return
        # ... باقي الراوترات إن لزم ...

    # ابدأ اللعب من الصفر — يبدأ السؤال الأول مباشرة وبنفس الشاشة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_startover")
    def on_startover(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id, "تم بدء لعبة جديدة: تصفير النقاط وحذف التقدّم.")
        except: pass

        # أوقف مؤقّت سابق
        rt_prev = get_runtime(user_id)
        cancel_prev = rt_prev.get("timer_cancel")
        if cancel_prev:
            try: cancel_prev.set()
            except: pass
        clear_runtime(user_id)

        # صفّر النقاط والتقدّم (الرصيد يبقى)
        wipe_user_for_fresh_start(user_id)

        # خصم هذه المحاولة إن لزم ثم عرض السؤال الأول فورًا
        ok, bal_or_new, price, reason = ensure_paid_before_show(user_id)
        if not ok:
            try: bot.answer_callback_query(call.id, "رصيدك غير كافٍ لهذه المحاولة.", show_alert=True)
            except: pass
            # أعِد شاشة البداية بنفس الرسالة
            _intro_screen(bot, chat_id, user_id)
            return

        st, item, stage_no, q_idx = next_question(user_id)
        settings = load_settings()

        seconds_total = get_stage_time(stage_no, settings)
        remain = int(seconds_total)

        kb = _question_markup(item)
        show_charge_line = (reason in ("paid", "already"))
        txt = _question_text(
            item, stage_no, q_idx, remain, seconds_total, settings,
            show_charge_line=show_charge_line, bal_after_charge=(bal_or_new if show_charge_line else None)
        )
        msg_id = _edit_or_send(bot, chat_id, st, txt, kb)

        st["active_msg_id"] = msg_id
        st["started_at"] = time.time()
        st["attempts_on_current"] = 0  # بداية سؤال جديد
        user_quiz_state[user_id] = st
        persist_state(user_id)

        # مؤقّت يُحرّك العداد
        cancel = threading.Event()
        set_runtime(user_id, timer_cancel=cancel)
        tick = max(1, int(settings.get("timer_tick_seconds", 5)))

        def _timer():
            nonlocal remain
            while remain > 0 and not cancel.is_set():
                time.sleep(tick)
                remain = max(0, remain - tick)
                if cancel.is_set():
                    return
                try:
                    new_txt = _question_text(
                        item, stage_no, q_idx, remain, seconds_total, settings,
                        show_charge_line=show_charge_line, bal_after_charge=(bal_or_new if show_charge_line else None)
                    )
                    bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=new_txt, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    pass

            if cancel.is_set():
                return

            # انتهى الوقت ⇒ خطأ + مسح paid_key + إلغاء إعفاء الخصم
            register_wrong_attempt(user_id)
            st_end = user_quiz_state.get(user_id) or {}
            st_end.pop("paid_key", None)
            st_end["no_charge_next"] = 0
            user_quiz_state[user_id] = st_end
            persist_state(user_id)

            wrong_line = _pick_banter("banter_wrong_by_stage", stage_no, settings)
            price_now = get_attempt_price(stage_no, settings)
            bal_now, pts_now = get_wallet(user_id)
            text_err = _fmt_error("timeout", price_now, settings, wrong_line, bal_now, pts_now)
            try:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text_err, parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="🔁 إعادة المحاولة", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass

        threading.Thread(target=_timer, daemon=True).start()

    # نقاطي — شاشة واحدة (تحرير الرسالة)
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_points")
    def on_points(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        bal, pts = get_wallet(user_id)
        syp_val = get_points_value_syp(pts)
        try: bot.answer_callback_query(call.id)
        except: pass
        st = user_quiz_state.get(user_id) or get_progress(user_id) or reset_progress(user_id)
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
        text = f"🏅 نقاطك: <b>{pts}</b> (≈ {syp_val} ل.س)\n💰 رصيدك: <b>{bal}</b> ل.س"
        msg_id = _edit_or_send(bot, chat_id, st, text, kb)
        st["active_msg_id"] = msg_id; user_quiz_state[user_id] = st; persist_state(user_id)

    # تحويل النقاط إلى رصيد — Alert دائمًا
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_convert")
    def on_convert(call):
        user_id = call.from_user.id
        try:
            pts_before, syp_added, pts_after = convert_points_to_balance(user_id)
            if syp_added <= 0:
                bot.answer_callback_query(call.id, "لا توجد نقاط كافية للتحويل.", show_alert=True)
            else:
                bot.answer_callback_query(
                    call.id,
                    f"✅ تم التحويل!\nحُوِّل {pts_before - pts_after} نقطة إلى {syp_added} ل.س.\nنقاطك الآن: {pts_after}.",
                    show_alert=True
                )
        except Exception:
            try: bot.answer_callback_query(call.id, "تعذّر التحويل مؤقتًا. حاول لاحقًا.", show_alert=True)
            except: pass

    # الترتيب — شاشة واحدة (تحرير الرسالة)
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_rank")
    def on_rank(call):
        chat_id = call.message.chat.id
        user_id = call.from_user.id
        try: bot.answer_callback_query(call.id)
        except: pass
        from services.quiz_service import get_leaderboard_by_progress
        top = get_leaderboard_by_progress(10)
        st = user_quiz_state.get(user_id) or get_progress(user_id) or reset_progress(user_id)
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
        if not top:
            text = "لا توجد بيانات ترتيب بعد."
        else:
            lines = ["🏆 <b>الترتيب حسب التقدّم</b>"]
            for i, row in enumerate(top, start=1):
                nm = row.get("name") or f"UID{row.get('user_id')}"
                stg = row.get("stage", 0); done = row.get("stage_done", 0)
                lines.append(f"{i}. <b>{nm}</b> — مرحلة <b>{stg}</b>، منجز <b>{done}</b> سؤالًا")
            text = "\n".join(lines)
        msg_id = _edit_or_send(bot, chat_id, st, text, kb)
        st["active_msg_id"] = msg_id; user_quiz_state[user_id] = st; persist_state(user_id)

    # شرح — شاشة واحدة (تحرير الرسالة)
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_help")
    def on_help(call):
        user_id = call.from_user.id
        try: bot.answer_callback_query(call.id)
        except: pass
        st = user_quiz_state.get(user_id) or get_progress(user_id) or reset_progress(user_id)
        kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
        chat_id = call.message.chat.id
        msg_id = _edit_or_send(bot, chat_id, st, _help_text(load_settings()), kb)
        st["active_msg_id"] = msg_id; user_quiz_state[user_id] = st; persist_state(user_id)

    # التالي/متابعة — شاشة واحدة
    @bot.callback_query_handler(func=lambda c: c.data in ("quiz_next", "quiz_resume"))
    def on_next(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        ensure_user_wallet(user_id)

        # أوقف مؤقّت سابق
        rt_prev = get_runtime(user_id)
        cancel_prev = rt_prev.get("timer_cancel")
        if cancel_prev:
            try: cancel_prev.set()
            except: pass

        # خصم هذه المحاولة إن لزم (يحترم no_charge_next)
        ok, bal_or_new, price, reason = ensure_paid_before_show(user_id)
        if not ok:
            try: bot.answer_callback_query(call.id, "رصيدك غير كافٍ لهذه المحاولة.", show_alert=True)
            except: pass
            return

        st, item, stage_no, q_idx = next_question(user_id)
        settings = load_settings()

        seconds_total = get_stage_time(stage_no, settings)
        remain = int(seconds_total)

        kb = _question_markup(item)
        # نظهر سطر "تم الخصم" فقط إن كان السبب paid/already
        show_charge_line = (reason in ("paid", "already"))
        txt = _question_text(
            item, stage_no, q_idx, remain, seconds_total, settings,
            show_charge_line=show_charge_line, bal_after_charge=(bal_or_new if show_charge_line else None)
        )
        msg_id = _edit_or_send(bot, chat_id, st, txt, kb)

        st["active_msg_id"] = msg_id
        st["started_at"] = time.time()
        st["attempts_on_current"] = 0  # بداية سؤال جديد
        user_quiz_state[user_id] = st
        persist_state(user_id)

        # مؤقّت يُحرّك العداد
        cancel = threading.Event()
        set_runtime(user_id, timer_cancel=cancel)
        tick = max(1, int(settings.get("timer_tick_seconds", 5)))

        def _timer():
            nonlocal remain
            while remain > 0 and not cancel.is_set():
                time.sleep(tick)
                remain = max(0, remain - tick)
                if cancel.is_set():
                    return
                try:
                    new_txt = _question_text(
                        item, stage_no, q_idx, remain, seconds_total, settings,
                        show_charge_line=show_charge_line, bal_after_charge=(bal_or_new if show_charge_line else None)
                    )
                    bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=new_txt, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    pass

            if cancel.is_set():
                return

            # انتهى الوقت ⇒ خطأ + مسح paid_key + إلغاء إعفاء الخصم
            register_wrong_attempt(user_id)
            st_end = user_quiz_state.get(user_id) or {}
            st_end.pop("paid_key", None)
            st_end["no_charge_next"] = 0
            user_quiz_state[user_id] = st_end
            persist_state(user_id)

            wrong_line = _pick_banter("banter_wrong_by_stage", stage_no, settings)
            price_now = get_attempt_price(stage_no, settings)
            bal_now, pts_now = get_wallet(user_id)
            text_err = _fmt_error("timeout", price_now, settings, wrong_line, bal_now, pts_now)
            try:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text_err, parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="🔁 إعادة المحاولة", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass

        threading.Thread(target=_timer, daemon=True).start()

    # اختيار الإجابة
    @bot.callback_query_handler(func=lambda c: c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass

        # أوقف المؤقّت الجاري
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

        settings = load_settings()

        if chosen != int(item.get("correct_index", -1)):
            # خطأ ⇒ تحرير نفس الرسالة + مسح paid_key + إلغاء إعفاء الخصم
            register_wrong_attempt(user_id)
            st_bad = user_quiz_state.get(user_id) or {}
            st_bad.pop("paid_key", None)
            st_bad["no_charge_next"] = 0
            user_quiz_state[user_id] = st_bad
            persist_state(user_id)

            wrong_line = _pick_banter("banter_wrong_by_stage", stage_no, settings)
            price_now = get_attempt_price(stage_no, settings)
            bal_now, pts_now = get_wallet(user_id)
            text_err = _fmt_error("wrong", price_now, settings, wrong_line, bal_now, pts_now)
            try:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text_err, parse_mode="HTML",
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
        # منح نقاط فورية بحسب ترتيب المحاولة لهذا السؤال، وتسجيلها في transactions
        delta_pts, pts_now, bal_now = award_points_for_correct(user_id, st["template_id"], stage_no, item, q_idx)

        # تحديث التقدّم: انتقل للسؤال التالي وصفّر عدّاد محاولات السؤال
        tpl = load_template(st["template_id"])
        total_q = len(tpl.get("items_by_stage", {}).get(str(stage_no), []) or [])
        st["q_index"] = int(st.get("q_index", 0)) + 1
        st["attempts_on_current"] = 0
        user_quiz_state[user_id] = st
        persist_state(user_id)

        ok_line = _pick_banter("banter_correct_by_stage", stage_no, settings)

        if st["q_index"] >= total_q:
            # نهاية المرحلة
            result = compute_stage_reward_and_finalize(user_id, stage_no, total_q)
            bal_end, _pts_tmp = get_wallet(user_id)
            success_text = _fmt_success_end(result.get("reward_points", 0), result.get("points_after", pts_now), settings, ok_line, bal_end)
            try:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=success_text, parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="⏭️ السؤال التالي (بدون خصم)", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass
        else:
            # نجاح وسطي: إعفاء الخصم للسؤال التالي مُفعّل (إذا انتقل فورًا)
            mid_text = _fmt_success_mid(settings, ok_line, delta_pts, bal_now, pts_now)
            try:
                bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=mid_text, parse_mode="HTML",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(text="⏭️ السؤال التالي (بدون خصم)", callback_data="quiz_next"),
                        types.InlineKeyboardButton(text="⏸️ أكمل لاحقًا", callback_data="quiz_cancel"),
                    )
                )
            except Exception:
                pass

    # إلغاء — يعيد إلى "القائمة الرئيسية" بتحديث نفس الشاشة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass

        # أوقف أي مؤقّت
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass
        clear_runtime(user_id)

        # امسح paid_key وألغِ إعفاء الخصم + فك ارتباط الرسالة النشطة
        st = user_quiz_state.get(user_id) or {}
        msg_id = st.get("active_msg_id") or call.message.message_id
        st.pop("paid_key", None)
        st["no_charge_next"] = 0
        st["active_msg_id"] = None
        user_quiz_state[user_id] = st
        persist_state(user_id)

        # استبدال الواجهة الحالية بواجهة "القائمة الرئيسية" (بدون أزرار)
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="🏠 <b>القائمة الرئيسية</b>\nللبدء مجددًا أرسل «🎯 الحزازير (ربحي)» أو /quiz.",
                parse_mode="HTML",
                reply_markup=None
            )
        except Exception:
            # في حال تعذّر التحرير نحذف الرسالة للحفاظ على شاشة واحدة
            try: bot.delete_message(chat_id, msg_id)
            except Exception: pass

# توافق مع الاستدعاء في main.py
attach_handlers = wire_handlers
