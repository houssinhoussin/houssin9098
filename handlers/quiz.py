# handlers/quiz.py
# زر "🎯 الحزازير (ربحي)" + منطق السؤال والعدّاد 60 ثانية وتقييم الإجابة مع جوائز المرحلة
from __future__ import annotations
import threading
import time
from typing import Optional

from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_attempt_price,
    reset_progress, next_question, deduct_fee_for_stage, add_points,
    user_quiz_state,                # للحالة الدائمة (تُحفظ في القاعدة - أرقام/نصوص فقط)
    convert_points_to_balance,
    get_runtime, set_runtime, clear_runtime,   # حالة وقتية (RAM) مثل Event/التزامن
    load_template,
    compute_stage_reward_and_finalize,
)

# ------------------------ أدوات واجهة ------------------------
def _timer_bar(total: int, left: int, full: str, empty: str) -> str:
    # شريط بسيط بطول 10 خانات
    slots = 10
    filled = max(0, min(slots, round((left/total)*slots)))
    return full * filled + empty * (slots - filled)

def _question_text(stage_no: int, q_idx: int, item: dict, settings: dict, seconds_left: int) -> str:
    bar = _timer_bar(settings["seconds_per_question"], seconds_left, settings["timer_bar_full"], settings["timer_bar_empty"])
    return (
        f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
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

# ------------------------ مؤقّت التحديث (تحرير نفس الرسالة) ------------------------
def _start_timer(bot: TeleBot, chat_id: int, msg_id: int, user_id: int, settings: dict):
    total = int(settings["seconds_per_question"])
    tick  = int(settings["timer_tick_seconds"])

    # نخزن الـ Event في ذاكرة وقتية فقط (RAM) — لا تدخل Supabase
    cancel = threading.Event()
    set_runtime(user_id, timer_cancel=cancel, last_answer_ts=0)

    def _loop():
        left = total
        while left > 0 and not cancel.is_set():
            try:
                # إعادة طباعة نفس السؤال مع الشريط
                _, item, stage_no, q_idx = next_question(user_id)
                txt = _question_text(stage_no, q_idx, item, settings, left)
                kb  = _options_markup(item)
                bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            time.sleep(tick)
            left -= tick

        # ⌛ انتهى الوقت ولم تصل إجابة
        if not cancel.is_set():
            # نسجّل محاولة خاطئة ونزيد محاولات هذا السؤال
            st = user_quiz_state.get(user_id, {})
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            st["attempts_on_current"]  = int(st.get("attempts_on_current", 0)) + 1
            user_quiz_state[user_id] = st
            # أعد عرض نفس السؤال (سيخصم قبل العرض)
            _send_next_question(bot, chat_id, user_id)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# ------------------------ نقطة دخول: زر القائمة ------------------------
def attach_handlers(bot: TeleBot):

    @bot.message_handler(func=lambda m: m.text == "🎯 الحزازير (ربحي)")
    def quiz_home(msg):
        user_id = msg.from_user.id
        name = (msg.from_user.first_name or "").strip()
        ensure_user_wallet(user_id, name)

        # بدء مسار جديد (القالب المختار للمستخدم بحسب templates_order)
        st = reset_progress(user_id)
        # تهيئة عدادات المرحلة
        st["stage_stars"] = 0
        st["stage_wrong_attempts"] = 0
        st["stage_done"] = 0
        st["attempts_on_current"] = 0
        user_quiz_state[user_id] = st

        _send_next_question(bot, msg.chat.id, user_id)

    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int):
        settings = load_settings()

        # السؤال الحالي (لا يتقدم المؤشر هنا)
        st, item, stage_no, q_idx = next_question(user_id)

        # خصم السعر قبل إظهار السؤال (الدفع المسبق)
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

        # إرسال السؤال وبدء المؤقت لتحرير نفس الرسالة
        txt = _question_text(stage_no, q_idx, item, settings, settings["seconds_per_question"])
        kb  = _options_markup(item)
        sent = bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="HTML")

        # خزّن msg_id وبداية الوقت (قيم قابلة للتسلسل فقط)
        st["active_msg_id"] = sent.message_id
        st["started_at"] = int(time.time() * 1000)
        # لا تلمس attempts_on_current هنا؛ تبقى كما هي
        user_quiz_state[user_id] = st

        # شغّل المؤقت (تحرير نفس الرسالة)
        _start_timer(bot, chat_id, sent.message_id, user_id, settings)

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("quiz_ans:"))
    def on_answer(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        # Debounce: تجاهل نقرات متتالية خلال 1 ثانية
        rt = get_runtime(user_id)
        now = time.time()
        last = float(rt.get("last_answer_ts", 0))
        if now - last < 1.0:
            try:
                bot.answer_callback_query(call.id)  # إغلاق دوّامة التحميل
            except Exception:
                pass
            return
        set_runtime(user_id, last_answer_ts=now)

        # أوقف المؤقت (RAM فقط)
        cancel = rt.get("timer_cancel")
        if cancel:
            cancel.set()

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)
        idx = int(call.data.split(":")[1])

        # احسب الصحة
        is_correct = (idx == int(item["correct_index"]))

        # نجوم هذا السؤال تعتمد على عدد المحاولات قبل الإجابة الصحيحة
        # 3 نجوم لو أول محاولة، 2 لو كان في محاولة واحدة خاطئة، 1 لو محاولتين خاطئتين، 0 إن ≥3
        attempts_on_current = int(st.get("attempts_on_current", 0))

        if is_correct:
            # نجوم هذا السؤال
            stars_here = max(0, 3 - attempts_on_current)
            # نقاط حسب النجوم
            pmap = settings.get("points_per_stars", {"3": 3, "2": 2, "1": 1, "0": 0})
            award_pts = int(pmap.get(str(stars_here), stars_here))
            _, pts = add_points(user_id, award_pts)

            # حدّث عدادات المرحلة
            st["stage_stars"] = int(st.get("stage_stars", 0)) + stars_here
            st["stage_done"]  = int(st.get("stage_done", 0)) + 1
            st["attempts_on_current"] = 0  # صفّر للمسألة التالية
            user_quiz_state[user_id] = st

            # هل هذا آخر سؤال في المرحلة؟
            tpl = load_template(st["template_id"])
            items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
            is_last_in_stage = (q_idx == len(items) - 1)

            # نص نتيجة سريع
            result = f"✅ صحيح! (+{award_pts} نقاط) — نقاطك الآن: <b>{pts}</b>"

            # أظهر النتيجة على نفس الرسالة
            try:
                kb = _options_markup(item)  # نفس الكيبورد (لن يُستخدم بعد ثانيتين)
                txt = (
                    f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
                    f"{item['text']}\n\n{result}"
                )
                bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass

            # بعد ثانيتين:
            def _after_correct():
                from services.quiz_service import advance
                advance(user_id)  # تقدم إلى السؤال التالي (أو يتجاوز نهاية المرحلة)
                if is_last_in_stage:
                    # احسب مكافأة المرحلة قبل أن يصفّرها الانتقال الداخلي
                    summary = compute_stage_reward_and_finalize(user_id, stage_no, len(items))
                    # ملخص مرحلة
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

                # اطرح السؤال التالي (سيظهر بداية المرحلة التالية تلقائيًا)
                _send_next_question(bot, chat_id, user_id)

            threading.Timer(2.0, _after_correct).start()

        else:
            # خاطئة: زد عداد الأخطاء وعدد محاولات هذا السؤال
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            st["attempts_on_current"]  = attempts_on_current + 1
            user_quiz_state[user_id] = st

            # أظهر نتيجة سريعة
            try:
                kb = _options_markup(item)
                txt = (
                    f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
                    f"{item['text']}\n\n"
                    f"❌ إجابة خاطئة. سيتم خصم كلفة محاولة جديدة عند الإعادة…"
                )
                bot.edit_message_text(txt, chat_id, call.message.message_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass

            # بعد لحظتين، أعد نفس السؤال (سيخصم قبل العرض)
            def _after_wrong():
                _send_next_question(bot, chat_id, user_id)

            threading.Timer(1.5, _after_wrong).start()

    @bot.callback_query_handler(func=lambda c: c.data == "quiz_convert")
    def on_convert(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        pts_before, syp_added, pts_after = convert_points_to_balance(user_id)
        if syp_added <= 0:
            try:
                bot.answer_callback_query(call.id, "لا توجد نقاط كافية للتحويل.", show_alert=True)
            except Exception:
                pass
            return

        try:
            bot.answer_callback_query(call.id, "تم التحويل!", show_alert=False)
        except Exception:
            pass

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
