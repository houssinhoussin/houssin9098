# handlers/quiz.py
# يضيف زر "🎯 تحدّي الجوائز" + منطق عرض السؤال والعداد 60 ثانية وتقييم الإجابة
from __future__ import annotations
import threading
import time
from typing import Optional

from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_attempt_price,
    reset_progress, next_question, deduct_fee_for_stage, add_points,
)
from services.quiz_service import user_quiz_state   # للوصول إلى حالة المؤقت والرسالة
from services.quiz_service import convert_points_to_balance

# ------------------------ أدوات واجهة ------------------------
def _timer_bar(total: int, left: int, full: str, empty: str) -> str:
    # طول الشريط = 10 خانات
    slots = 10
    filled = max(0, min(slots, round((left/total)*slots)))
    return full * filled + empty * (slots - filled)

def _question_text(stage_no: int, q_idx: int, item: dict, settings: dict, seconds_left: int) -> str:
    bar = _timer_bar(settings["seconds_per_question"], seconds_left, settings["timer_bar_full"], settings["timer_bar_empty"])
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال رقم <b>{q_idx+1}</b>\n"
        f"⏳ {seconds_left}s {bar}\n\n"
        f"{item['text']}"
    )

def _options_markup(item: dict) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    opts = item["options"]
    buttons = []
    for i, o in enumerate(opts):
        buttons.append(types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}"))
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(text="💳 تحويل النقاط إلى رصيد", callback_data="quiz_convert"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

# ------------------------ مؤقّت التحديث (بدون رسائل جديدة) ------------------------
def _start_timer(bot: TeleBot, chat_id: int, msg_id: int, user_id: int, settings: dict):
    total = int(settings["seconds_per_question"])
    tick  = int(settings["timer_tick_seconds"])
    # خزن cancel في الحالة لإيقافه عند الإجابة
    cancel = threading.Event()
    st = user_quiz_state.get(user_id, {})
    st["timer_cancel"] = cancel
    user_quiz_state[user_id] = st

    def _loop():
        left = total
        while left > 0 and not cancel.is_set():
            try:
                # نقرأ السؤال الحالي لإعادة طباعة النص مع الشريط
                _, item, stage_no, q_idx = next_question(user_id)
                txt = _question_text(stage_no, q_idx, item, settings, left)
                kb  = _options_markup(item)
                bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            time.sleep(tick)
            left -= tick
        # عند انتهاء الوقت ولم تُجب بعد، لا نفعل شيئًا هنا:
        # المعالجة النهائية تتم عند ضغطة اللاعب أو استدعاء إعادة السؤال.

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# ------------------------ نقطة دخول: زر القائمة ------------------------
def attach_handlers(bot: TeleBot):

    @bot.message_handler(func=lambda m: m.text and "🎯" in m.text)
    def quiz_home(msg):
        user_id = msg.from_user.id
        name = (msg.from_user.first_name or "").strip()
        ensure_user_wallet(user_id, name)

        st = reset_progress(user_id)  # بداية جديدة (نفس القالب المختار للمستخدم)
        _send_next_question(bot, msg.chat.id, user_id)

    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int):
        settings = load_settings()
        # خصم السعر قبل إظهار السؤال
        st, item, stage_no, q_idx = next_question(user_id)
        ok, new_bal, price = deduct_fee_for_stage(user_id, stage_no)
        if not ok:
            bal, pts = get_wallet(user_id)
            bot.send_message(chat_id,
                f"❌ رصيدك غير كافٍ لسعر السؤال.\n"
                f"السعر المطلوب: <b>{price}</b> ل.س\n"
                f"رصيدك المتاح: <b>{bal}</b> ل.س",
                parse_mode="HTML"
            )
            return

        txt = _question_text(stage_no, q_idx, item, settings, settings["seconds_per_question"])
        kb  = _options_markup(item)
        sent = bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")

        # خزّن msg_id لنعيد تحرير نفس الرسالة
        st["active_msg_id"] = sent.message_id
        st["started_at"] = int(time.time()*1000)
        user_quiz_state[user_id] = st

        # شغّل المؤقت (تحرير نفس الرسالة)
        _start_timer(bot, chat_id, sent.message_id, user_id, settings)

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        idx = int(call.data.split(":")[1])

        # أوقف المؤقت
        st = user_quiz_state.get(user_id, {})
        cancel = st.get("timer_cancel")
        if cancel:
            cancel.set()

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        is_correct = (idx == int(item["correct_index"]))
        # منح النقاط حسب الصعوبة
        diff = item.get("difficulty", "medium")
        stars_map = settings.get("points_per_stars", {"3": 3, "2": 2, "1": 1, "0": 0})
        award = 1 if diff == "easy" else (2 if diff == "medium" else 3)
        if is_correct:
            _, pts = add_points(user_id, award)
            result = f"✅ إجابة صحيحة! (+{award} نقاط) — نقاطك الآن: <b>{pts}</b>"
            # تقدّم للسؤال التالي
            from services.quiz_service import advance
            advance(user_id)
        else:
            result = "❌ إجابة خاطئة. جرّب من جديد…"
            # سيُعاد نفس السؤال وسيُخصم عند العرض القادم تلقائيًا

        kb = _options_markup(item)
        txt = (
            f"🎯 <b>المرحلة {stage_no}</b> — السؤال رقم <b>{q_idx+1}</b>\n"
            f"{item['text']}\n\n"
            f"{result}"
        )
        bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb, parse_mode="HTML")

        # بعد ثانيتين، أعرض السؤال التالي/المعاد
        def _after():
            if is_correct:
                _send_next_question(bot, chat_id, user_id)
            else:
                _send_next_question(bot, chat_id, user_id)
        threading.Timer(2.0, _after).start()

    @bot.callback_query_handler(func=lambda c: c.data == "quiz_convert")
    def on_convert(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        pts_before, syp_added, pts_after = convert_points_to_balance(user_id)
        if syp_added <= 0:
            bot.answer_callback_query(call.id, "لا توجد نقاط كافية للتحويل.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "تم التحويل!", show_alert=False)
        bot.send_message(
            chat_id,
            f"💳 تم تحويل <b>{pts_before}</b> نقطة إلى <b>{syp_added}</b> ل.س.\n"
            f"نقاطك الآن: <b>{pts_after}</b>.",
            parse_mode="HTML"
        )

    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        user_id = call.from_user.id
        st = user_quiz_state.get(user_id, {})
        cancel = st.get("timer_cancel")
        if cancel:
            cancel.set()
        bot.answer_callback_query(call.id, "تم الإلغاء.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
