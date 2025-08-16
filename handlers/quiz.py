# handlers/quiz.py
# "🎯 الحزازير (ربحي)" بدون عدّاد
# - يحذف رسالة السؤال فور التقييم (صح/خطأ)
# - بعد الصح: رسالة نجاح + زر ⏭️ التالي (يخصم عند العرض)
# - بعد الخطأ: رسالة خطأ + زر 🔁 إعادة المحاولة (يخصم) + ⏸️ أكمل لاحقًا
# - زر "🏅 نقاطي" يظهر النقاط + ما تعادل بالليرة + رصيد المحفظة
# - شاشة الملخّص تدعم مكافأة نقاط أو ليرات (تلقائيًا حسب مخرجات الخدمة الحالية)

from __future__ import annotations
import time

from telebot import TeleBot, types

from services.quiz_service import (
    # اقتصاد/حالة
    load_settings, ensure_user_wallet, get_wallet, get_attempt_price,
    reset_progress, next_question, deduct_fee_for_stage, add_points,
    user_quiz_state, convert_points_to_balance, load_template, advance,
    get_points_value_syp, compute_stage_reward_and_finalize,
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
    kb.add(types.InlineKeyboardButton(text=f"🔁 إعادة المحاولة (سيُخصم {price} ل.س)", callback_data="quiz_retry"))
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

def _delete_if_exists(bot: TeleBot, chat_id: int, msg_id: int | None):
    if not msg_id:
        return
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        # fallback: عطّل الأزرار إن فشل الحذف (رسالة قديمة، لا صلاحية، إلخ)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass

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

    resume_avail = (int(st.get("q_index", 0)) > 0 or bool(st.get("active_msg_id")))

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
        # ✳️ لا نعيد ضبط التقدم تلقائيًا؛ ليقدر يكمل لاحقًا
        if not user_quiz_state.get(user_id):
            reset_progress(user_id)
        # نظّف أي رسالة معلومات قديمة مُخزنة
        st = user_quiz_state.get(user_id, {})
        st.pop("last_info_msg_id", None)
        user_quiz_state[user_id] = st
        _intro_screen(bot, msg.chat.id, user_id)

    # --------------------------------------------
    # عرض السؤال (خصم مسبق) — يُستدعى من: quiz_next / quiz_retry / quiz_resume
    # يحاول حذف الرسالة السابقة (سؤال/نتيجة) قبل طرح السؤال الجديد
    # --------------------------------------------
    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int,
                            delete_msg_ids: list[int] | None = None,
                            skip_deduct: bool = False) -> bool:
        st, item, stage_no, q_idx = next_question(user_id)

        # خصم السعر قبل الإظهار (إلا في حالة الاستئناف)
        if not skip_deduct:
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

        # احذف أي رسائل قديمة (سؤال/نتيجة) قبل طرح السؤال الجديد
        if delete_msg_ids:
            for mid in delete_msg_ids:
                _delete_if_exists(bot, chat_id, mid)

        # احذف أيضًا السؤال النشط السابق إن وُجد
        _delete_if_exists(bot, chat_id, st.get("active_msg_id"))

        # أرسل السؤال الجديد
        txt = _question_text(stage_no, q_idx, item)
        sent = bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=_question_markup(item))

        # حدث الحالة
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

        delete_ids = [call.message.message_id]
        # الاستئناف لا يخصم (نعيد إرسال نفس السؤال لمن فاته الرسالة)
        skip = (call.data == "quiz_resume")
        _send_next_question(bot, chat_id, user_id, delete_msg_ids=delete_ids, skip_deduct=skip)

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

        # احذف رسالة السؤال فورًا
        _delete_if_exists(bot, chat_id, st.get("active_msg_id"))
        # احذف رسالة معلومات قديمة إن وُجدت
        _delete_if_exists(bot, chat_id, st.get("last_info_msg_id"))
        st.pop("active_msg_id", None)
        st.pop("last_info_msg_id", None)

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

            # هل هذا آخر سؤال في المرحلة؟
            tpl = load_template(st["template_id"])
            items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
            is_last_in_stage = (q_idx == len(items) - 1)

            # رسالة نجاح + زر ⏭️ التالي
            ok_msg = bot.send_message(
                chat_id,
                f"✅ إجابة صحيحة! (+{award_pts} نقاط)\n"
                f"🏅 نقاطك الآن: <b>{pts}</b>",
                parse_mode="HTML",
                reply_markup=_after_correct_markup()
            )
            st["last_info_msg_id"] = ok_msg.message_id
            user_quiz_state[user_id] = st

            # تقدّم بالمؤشر (العرض الفعلي عند الضغط على «التالي»)
            advance(user_id)

            # نهاية مرحلة → ملخص فوري (الجائزة قد تكون نقاط أو ليرات حسب الخدمة الحالية)
            if is_last_in_stage:
                summary = compute_stage_reward_and_finalize(user_id, stage_no, len(items))
                # دعم مفتاحي points أو syp
                reward_points = summary.get("reward_points")
                points_after  = summary.get("points_after")
                reward_syp    = summary.get("reward_syp")
                balance_after = summary.get("balance_after")

                if reward_points is not None:
                    bot.send_message(
                        chat_id,
                        "🏁 <b>ملخص المرحلة</b>\n"
                        f"المرحلة: <b>{stage_no}</b>\n"
                        f"الأسئلة المنجزة: <b>{summary.get('questions', 0)}</b>\n"
                        f"المحاولات الخاطئة: <b>{summary.get('wrong_attempts', 0)}</b>\n"
                        f"النجوم: <b>{summary.get('stars', 0)}</b>\n"
                        f"🎁 الجائزة: <b>{reward_points}</b> نقطة\n"
                        f"🏅 نقاطك الآن: <b>{points_after}</b>",
                        parse_mode="HTML"
                    )
                else:
                    # تراجع مؤقتًا على النسخة القديمة (بالليرة) لحين تعديل الخدمة
                    bot.send_message(
                        chat_id,
                        "🏁 <b>ملخص المرحلة</b>\n"
                        f"المرحلة: <b>{stage_no}</b>\n"
                        f"الأسئلة المنجزة: <b>{summary.get('questions', 0)}</b>\n"
                        f"المحاولات الخاطئة: <b>{summary.get('wrong_attempts', 0)}</b>\n"
                        f"النجوم: <b>{summary.get('stars', 0)}</b>\n"
                        f"🎁 الجائزة: <b>{reward_syp or 0}</b> ل.س\n"
                        f"💰 رصيدك الآن: <b>{balance_after or 0}</b> ل.س",
                        parse_mode="HTML"
                    )

        else:
            # خطأ → حدّث عدادات المرحلة
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

    # تحويل النقاط
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

    # عرض النقاط + رصيد المحفظة
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_points")
    def on_points(call):
        user_id = call.from_user.id
        settings = load_settings()
        bal, pts = get_wallet(user_id)
        syp_val  = get_points_value_syp(pts, settings)
        try:
            bot.answer_callback_query(
                call.id,
                f"🏅 نقاطك: {pts} (≈ {syp_val} ل.س)\n💰 رصيد المحفظة: {bal} ل.س",
                show_alert=False
            )
        except:
            pass

    # شرح اللعبة (بدون عدّاد)
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
            f"• مثال السعر (مرحلة 1): {price_hint} ل.س/محاولة (قابل للتغيّر حسب المرحلة)."
        )
        bot.send_message(call.message.chat.id, msg, parse_mode="HTML")

    # إيقاف مؤقت: رجوع لبداية زر الحزازير
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_pause")
    def on_pause(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id, "تم الحفظ. رجعناك لبداية الزر.")
        except: pass

        st = user_quiz_state.get(user_id, {}) or {}
        # احذف رسالة النتيجة الأخيرة إن وجدت
        _delete_if_exists(bot, chat_id, st.get("last_info_msg_id"))
        st.pop("last_info_msg_id", None)
        user_quiz_state[user_id] = st

        _intro_screen(bot, chat_id, user_id, resume_only=False)

    # إلغاء من الشاشة التمهيدية
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        try: bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
