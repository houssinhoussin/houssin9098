# handlers/quiz.py
from __future__ import annotations
import threading
import time
import random
from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet,
    reset_progress, next_question, deduct_fee_for_stage,
    add_points, advance, convert_points_to_balance,
    get_runtime, set_runtime, clear_runtime,
    stage_question_count, compute_stage_reward_and_finalize,
)

# --------------- واجهة ---------------
def _timer_bar(total: int, left: int, full: str, empty: str) -> str:
    slots = 12
    filled = max(0, min(slots, round((left/total)*slots)))
    return full * filled + empty * (slots - filled)

def _question_text(stage_no: int, q_idx: int, item: dict, settings: dict, seconds_left: int) -> str:
    bar = _timer_bar(settings["seconds_per_question"], seconds_left, settings["timer_bar_full"], settings["timer_bar_empty"])
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال رقم <b>{q_idx+1}</b>\n"
        f"⏳ {seconds_left}s {bar}\n\n"
        f"{item['text']}"
    )

def _options_markup(options: list[str]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}") for i, o in enumerate(options)])
    kb.add(types.InlineKeyboardButton(text="💳 تحويل النقاط إلى رصيد", callback_data="quiz_convert"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

# --------------- مؤقّت بدون رسائل جديدة ---------------
def _start_timer(bot: TeleBot, chat_id: int, msg_id: int, user_id: int, settings: dict):
    total = int(settings["seconds_per_question"])
    tick  = int(settings["timer_tick_seconds"])

    cancel = threading.Event()
    set_runtime(user_id, timer_cancel=cancel)

    def _loop():
        left = total
        while left > 0 and not cancel.is_set():
            try:
                st, item, stage_no, q_idx = next_question(user_id)
                perm = st.get("perm") or list(range(len(item["options"])))
                option_texts = [item["options"][i] for i in perm]
                bot.edit_message_text(
                    _question_text(stage_no, q_idx, item, settings, left),
                    chat_id, msg_id, reply_markup=_options_markup(option_texts), parse_mode="HTML"
                )
            except Exception:
                pass
            time.sleep(tick); left -= tick

        # ⌛ انتهى الوقت: تعتبر خاطئة وتُعاد نفس السؤال (سيُخصم قبل العرض القادم)
        if not cancel.is_set():
            try:
                st, _, stage_no, _ = next_question(user_id)
                st["attempts_on_question"] = int(st.get("attempts_on_question", 0)) + 1
                st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
                from services.quiz_service import user_quiz_state
                user_quiz_state[user_id] = st
                _send_next_question(bot, chat_id, user_id, timed_out=True)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()

# --------------- نقطة دخول ---------------
def attach_handlers(bot: TeleBot):

    @bot.message_handler(func=lambda m: m.text and "🎯" in m.text)
    def quiz_home(msg):
        user_id = msg.from_user.id
        ensure_user_wallet(user_id, (msg.from_user.first_name or "").strip())
        reset_progress(user_id)              # بداية جديدة لنفس القالب
        _send_next_question(bot, msg.chat.id, user_id, first=True)

    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int, first: bool=False, timed_out: bool=False):
        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        # خصم قبل العرض
        ok, new_bal, price = deduct_fee_for_stage(user_id, stage_no)
        if not ok:
            bal, _ = get_wallet(user_id)
            bot.send_message(chat_id,
                f"❌ رصيدك غير كافٍ لسعر السؤال.\nالسعر: <b>{price}</b> ل.س — رصيدك: <b>{bal}</b> ل.س",
                parse_mode="HTML"
            )
            return

        # عدّاد محاولات السؤال الحالي
        cur_key = f"{stage_no}:{q_idx}"
        if st.get("q_key") != cur_key:
            st["attempts_on_question"] = 0
        st["q_key"] = cur_key

        # خلط ترتيب الخيارات
        n = len(item["options"])
        perm = list(range(n)); random.shuffle(perm)
        st["perm"] = perm

        # تخزين
        from services.quiz_service import user_quiz_state
        user_quiz_state[user_id] = st

        # نص + كيبورد
        txt = _question_text(stage_no, q_idx, item, settings, settings["seconds_per_question"])
        if timed_out: txt = "⌛ <b>انتهى الوقت</b> — اعتُبرت المحاولة خاطئة.\n\n" + txt
        sent = bot.send_message(chat_id, txt, reply_markup=_options_markup([item["options"][i] for i in perm]), parse_mode="HTML")

        st["active_msg_id"] = sent.message_id
        st["started_at"] = int(time.time()*1000)
        user_quiz_state[user_id] = st
        _start_timer(bot, chat_id, sent.message_id, user_id, settings)

    # ---------- الإجابة ----------
    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        # Debounce 1s
        now = time.time()
        rt = get_runtime(user_id)
        if (now - float(rt.get("ans_ts", 0))) < 1.0:
            try: bot.answer_callback_query(call.id)
            except: pass
            return
        set_runtime(user_id, ans_ts=now)

        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        # تحويل فهرس العرض إلى الفهرس الأصلي
        try: display_idx = int(call.data.split(":")[1])
        except: display_idx = 0
        perm = st.get("perm") or list(range(len(item["options"])))
        original_idx = perm[display_idx] if 0 <= display_idx < len(perm) else display_idx

        is_correct = (original_idx == int(item["correct_index"]))
        attempts = int(st.get("attempts_on_question", 0))

        if is_correct:
            # نجوم حسب عدد المحاولات السابقة
            stars = 3 if attempts == 0 else (2 if attempts == 1 else (1 if attempts == 2 else 0))
            if stars > 0: add_points(user_id, stars)

            # تراكم نجوم المرحلة + عدد الأسئلة المُنجزة
            st["stage_stars"] = int(st.get("stage_stars", 0)) + stars
            st["stage_done"]  = int(st.get("stage_done", 0)) + 1
            st["attempts_on_question"] = 0
            from services.quiz_service import user_quiz_state
            user_quiz_state[user_id] = st

            # عرض “صح” سريع
            try: bot.answer_callback_query(call.id, "صحيح!")
            except: pass
            try:
                bot.edit_message_text(
                    f"🎯 <b>المرحلة {stage_no}</b> — السؤال {q_idx+1}\n{item['text']}\n\n✅ إجابة صحيحة!",
                    chat_id, call.message.message_id,
                    reply_markup=_options_markup([item["options"][i] for i in perm]),
                    parse_mode="HTML"
                )
            except: pass

            # هل اكتملت المرحلة؟
            total_q = stage_question_count(stage_no)
            if st["stage_done"] >= total_q:
                # احسب الجائزة واملأ الملخّص وابدأ المرحلة التالية
                def _finish():
                    summary = compute_stage_reward_and_finalize(user_id, stage_no, total_q)
                    txt = (
                        f"🏁 <b>انتهت المرحلة {stage_no}</b>\n\n"
                        f"• الأسئلة: <b>{summary['questions']}</b>\n"
                        f"• المحاولات الخاطئة: <b>{summary['wrong_attempts']}</b>\n"
                        f"• النجوم: <b>{summary['stars']}</b> / {3*summary['questions']}\n"
                        f"• الجائزة: <b>{summary['reward_syp']}</b> ل.س\n"
                        f"• رصيدك الآن: <b>{summary['balance_after']}</b> ل.س\n\n"
                        f"اضغط للمتابعة إلى المرحلة التالية."
                    )
                    kb = types.InlineKeyboardMarkup()
                    kb.add(types.InlineKeyboardButton("▶️ ابدأ المرحلة التالية", callback_data="quiz_next_stage"))
                    bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
                threading.Timer(1.0, _finish).start()
            else:
                # سؤال جديد
                threading.Timer(1.0, lambda: _send_next_question(bot, chat_id, user_id)).start()

        else:
            # خطأ: زِد عدّاد محاولات السؤال + الخاطئة في المرحلة
            st["attempts_on_question"] = attempts + 1
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            from services.quiz_service import user_quiz_state
            user_quiz_state[user_id] = st
            try: bot.answer_callback_query(call.id, "خاطئة، جرّب مجددًا")
            except: pass
            try:
                bot.edit_message_text(
                    f"🎯 <b>المرحلة {stage_no}</b> — السؤال {q_idx+1}\n{item['text']}\n\n❌ إجابة خاطئة. سيُعاد السؤال…",
                    chat_id, call.message.message_id,
                    reply_markup=_options_markup([item["options"][i] for i in perm]),
                    parse_mode="HTML"
                )
            except: pass
            threading.Timer(1.0, lambda: _send_next_question(bot, chat_id, user_id)).start()

    # ---------- تحويل نقاط ----------
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_convert")
    def on_convert(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        pts_before, syp_added, pts_after = convert_points_to_balance(user_id)
        if syp_added <= 0:
            bot.answer_callback_query(call.id, "لا توجد نقاط كافية للتحويل.", show_alert=True); return
        bot.answer_callback_query(call.id, "تم التحويل!", show_alert=False)
        bot.send_message(chat_id,
            f"💳 حوّلنا <b>{pts_before}</b> نقطة إلى <b>{syp_added}</b> ل.س.\nنقاطك الآن: <b>{pts_after}</b>.",
            parse_mode="HTML"
        )

    # ---------- إلغاء ----------
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        user_id = call.from_user.id
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel: cancel.set()
        clear_runtime(user_id)
        try: bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass

    # ---------- المرحلة التالية ----------
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_next_stage")
    def on_next_stage(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass
        _send_next_question(bot, chat_id, user_id)
