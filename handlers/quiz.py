# handlers/quiz.py
# يضيف زر "🎯 تحدّي الجوائز" + عدّاد 60 ثانية بتحرير نفس الرسالة + خصم قبل العرض
# + حساب النقاط بالنجوم (3/2/1/0) حسب عدد المحاولات على نفس السؤال
# + منع السبام على الإجابات + خلط ترتيب الخيارات كل إعادة
from __future__ import annotations
import threading
import time
import random
from typing import Optional

from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_attempt_price,
    reset_progress, next_question, deduct_fee_for_stage, add_points,
)
from services.quiz_service import user_quiz_state   # للحالة الدائمة (تُحفظ في القاعدة)
from services.quiz_service import convert_points_to_balance
from services.quiz_service import get_runtime, set_runtime, clear_runtime  # حالة وقتية (لا تُحفظ)

# ------------------------ أدوات واجهة ------------------------
def _timer_bar(total: int, left: int, full: str, empty: str) -> str:
    # طول الشريط = 12 خطوة (افتراضي: تحديث كل 5 ثواني لـ 60 ثانية)
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

def _options_markup(option_texts: list[str]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(text=o, callback_data=f"quiz_ans:{i}") for i, o in enumerate(option_texts)]
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(text="💳 تحويل النقاط إلى رصيد", callback_data="quiz_convert"))
    kb.add(types.InlineKeyboardButton(text="❌ إلغاء", callback_data="quiz_cancel"))
    return kb

# ------------------------ مؤقّت التحديث (بدون رسائل جديدة) ------------------------
def _start_timer(bot: TeleBot, chat_id: int, msg_id: int, user_id: int, settings: dict):
    total = int(settings["seconds_per_question"])
    tick  = int(settings["timer_tick_seconds"])

    # ✳️ خزّن الـ Event وقتيًا فقط (لا تُكتب في Supabase)
    cancel = threading.Event()
    set_runtime(user_id, timer_cancel=cancel)

    def _loop():
        left = total
        while left > 0 and not cancel.is_set():
            try:
                # نقرأ السؤال الحالي + ترتيب العرض الحالي من الحالة
                _st, item, stage_no, q_idx = next_question(user_id)
                st = user_quiz_state.get(user_id, {})
                perm = st.get("perm") or list(range(len(item["options"])))
                option_texts = [item["options"][i] for i in perm]
                txt = _question_text(stage_no, q_idx, item, settings, left)
                kb  = _options_markup(option_texts)
                bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            time.sleep(tick)
            left -= tick

        # ⌛ عند انتهاء الوقت ولم تُجب بعد: تُحسب محاولة خاطئة وتُعاد نفس السؤال (بدون خصم هنا،
        # لأن الخصم للدورة الجديدة سيتم قبل عرض السؤال القادم)
        if not cancel.is_set():
            try:
                # زد عدد المحاولات على هذا السؤال، وامنـع السبام المفتوح
                st = user_quiz_state.get(user_id, {})
                attempts = int(st.get("attempts_on_question", 0)) + 1
                st["attempts_on_question"] = attempts
                user_quiz_state[user_id] = st

                # أعد إرسال نفس السؤال (الدفع سيتم تلقائيًا قبل العرض القادم)
                _send_next_question(bot, chat_id, user_id, timed_out=True)
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# ------------------------ نقطة دخول: زر القائمة ------------------------
def attach_handlers(bot: TeleBot):

    @bot.message_handler(func=lambda m: m.text and "🎯" in m.text)
    def quiz_home(msg):
        user_id = msg.from_user.id
        name = (msg.from_user.first_name or "").strip()
        ensure_user_wallet(user_id, name)

        reset_progress(user_id)  # بداية جديدة (نفس القالب المختار للمستخدم)
        _send_next_question(bot, msg.chat.id, user_id, first=True)

    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int, first: bool=False, timed_out: bool=False):
        settings = load_settings()

        # جلب السؤال الحالي (لا يتقدم إلا بعد الإجابة الصحيحة)
        st, item, stage_no, q_idx = next_question(user_id)

        # ===== خصم السعر قبل إظهار السؤال (محاولة جديدة) =====
        ok, new_bal, price = deduct_fee_for_stage(user_id, stage_no)
        if not ok:
            bal, pts = get_wallet(user_id)
            bot.send_message(
                chat_id,
                f"❌ رصيدك غير كافٍ لسعر السؤال.\n"
                f"السعر المطلوب: <b>{price}</b> ل.س\n"
                f"رصيدك المتاح: <b>{bal}</b> ل.س",
                parse_mode="HTML"
            )
            return

        # ===== إدارة محاولات السؤال الحالي (للنجوم) =====
        # المعرّف المنطقي للسؤال الحالي
        cur_key = f"{stage_no}:{q_idx}"
        prev_key = st.get("q_key")
        if cur_key != prev_key:
            # سؤال جديد فعلاً → صفر عدّاد المحاولات
            st["attempts_on_question"] = 0
        st["q_key"] = cur_key

        # ===== خلط ترتيب الخيارات لكل عرض =====
        n = len(item["options"])
        perm = list(range(n))
        random.shuffle(perm)
        st["perm"] = perm  # للاستخدام عند الردّ والتحرير
        user_quiz_state[user_id] = st

        option_texts = [item["options"][i] for i in perm]

        # نص السؤال + الكيبورد
        txt = _question_text(stage_no, q_idx, item, settings, settings["seconds_per_question"])
        if timed_out:
            txt = "⌛ <b>انتهى الوقت</b> — اعتُبرت المحاولة خاطئة.\n\n" + txt
        kb  = _options_markup(option_texts)

        # إرسال/تحديث الرسالة
        sent = bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")

        # خزن msg_id لباقي التحريرات + بداية المؤقت
        st["active_msg_id"] = sent.message_id
        st["started_at"] = int(time.time()*1000)
        user_quiz_state[user_id] = st

        _start_timer(bot, chat_id, sent.message_id, user_id, settings)

    # ------------------------ معالجات الكول باك ------------------------
    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        # 🔒 Debounce بسيط: تجاهل الضغطات المتتالية في أقل من 1 ثانية
        now = time.time()
        rt = get_runtime(user_id)
        last_ts = float(rt.get("ans_ts", 0))
        if (now - last_ts) < 1.0:
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return
        set_runtime(user_id, ans_ts=now)

        # أوقف المؤقت (حالة وقتية فقط)
        cancel = rt.get("timer_cancel")
        if cancel:
            cancel.set()

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        # استخدم ترتيب العرض الحالي لتحويل الاختيار إلى فهرس حقيقي
        try:
            display_idx = int(call.data.split(":")[1])
        except Exception:
            display_idx = 0
        perm = st.get("perm") or list(range(len(item["options"])))
        original_idx = perm[display_idx] if 0 <= display_idx < len(perm) else display_idx

        is_correct = (original_idx == int(item["correct_index"]))

        # حساب النجوم/النقاط حسب عدد المحاولات على هذا السؤال
        attempts = int(st.get("attempts_on_question", 0))
        if is_correct:
            # نجوم: 0 محاولات سابقة = 3 نجوم، 1 = 2، 2 = 1، 3+ = 0
            stars = 3 if attempts == 0 else (2 if attempts == 1 else (1 if attempts == 2 else 0))
            award_pts = stars  # 3/2/1/0 نقاط
            if award_pts > 0:
                _, pts_total = add_points(user_id, award_pts)
            else:
                _, pts_total = get_wallet(user_id)  # لجلب الرصيد/النقاط الحالية بدون تغيير

            result = f"✅ إجابة صحيحة! (+{award_pts} نقاط) — نقاطك الآن: <b>{get_wallet(user_id)[1]}</b>"

            # تقدّم للسؤال التالي + صفّر عداد المحاولات
            from services.quiz_service import advance
            advance(user_id)
            st["attempts_on_question"] = 0
            user_quiz_state[user_id] = st

            # تحديت النص الحالي ثم عرض السؤال التالي بعد ثانية
            try:
                bot.answer_callback_query(call.id, "صحيح!")
            except Exception:
                pass

            try:
                txt = (
                    f"🎯 <b>المرحلة {stage_no}</b> — السؤال رقم <b>{q_idx+1}</b>\n"
                    f"{item['text']}\n\n"
                    f"{result}"
                )
                kb  = _options_markup([item["options"][i] for i in perm])
                bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass

            threading.Timer(1.0, lambda: _send_next_question(bot, chat_id, user_id)).start()

        else:
            # إجابة خاطئة → زد عداد المحاولات لهذا السؤال
            st["attempts_on_question"] = attempts + 1
            user_quiz_state[user_id] = st

            try:
                bot.answer_callback_query(call.id, "خاطئة، جرّب مجددًا")
            except Exception:
                pass

            # أعِد نفس السؤال (سيتم الخصم قبل العرض التالي تلقائيًا)
            # نُظهر رسالة صغيرة في نفس النص
            try:
                txt = (
                    f"🎯 <b>المرحلة {stage_no}</b> — السؤال رقم <b>{q_idx+1}</b>\n"
                    f"{item['text']}\n\n"
                    f"❌ إجابة خاطئة. سيُعاد السؤال…"
                )
                kb  = _options_markup([item["options"][i] for i in perm])
                bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass

            threading.Timer(1.0, lambda: _send_next_question(bot, chat_id, user_id)).start()

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
        # أوقف المؤقت من الحالة الوقتية ونظّفها
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            cancel.set()
        clear_runtime(user_id)

        try:
            bot.answer_callback_query(call.id, "تم الإلغاء.")
        except Exception:
            pass
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
