# handlers/quiz.py
# "🎯 الحزازير (ربحي)" مع عدّاد إيموجي يتحرّك بتحرير نفس الرسالة
# عند الصح: نحذف رسالة السؤال القديمة ثم نعرض رسالة نجاح + ⏭️ التالي
# عند الخطأ/انتهاء الوقت: رسالة خطأ + 🔁 إعادة المحاولة + ⏸️ أكمل لاحقًا

from __future__ import annotations
import time
import threading
import random

from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet, get_points_value_syp, get_attempt_price,
    reset_progress, next_question, add_points,
    user_quiz_state, convert_points_to_balance, load_template,
    compute_stage_reward_and_finalize, advance,
    get_runtime, set_runtime, clear_runtime,
    ensure_paid_before_show, pause_current_question, persist_state,
    get_seconds_for_stage,  # ✅ زمن السؤال حسب المرحلة
)

# ------------------------ أدوات واجهة ------------------------
def _timer_bar(total: int, left: int, full: str, empty: str) -> str:
    # شريط 12 خانة (كل خانة ~ خمس ثوانٍ تقريباً)
    slots = max(6, total // 5)
    filled = max(0, min(slots, round((left / total) * slots)))
    return full * filled + empty * (slots - filled)

def _question_text(stage_no: int, q_idx: int, item: dict, settings: dict, seconds_left: int, bal_hint: int | None = None) -> str:
    bar = _timer_bar(int(get_seconds_for_stage(stage_no, settings)), seconds_left, settings["ui"]["timer_bar_full"], settings["ui"]["timer_bar_empty"])
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

def _after_correct_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text="⏭️ التالي (سيُخصم عند العرض)", callback_data="quiz_next"))
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
        "• ٤ خيارات لكل سؤال + عدّاد وقت.\n"
        "• تُخصم كلفة <b>المحاولة</b> عند «ابدأ الآن/التالي» وأيضًا عند «إعادة المحاولة».\n"
        "• عند الخطأ/انتهاء الوقت، تعيد نفس السؤال (والخصم عند العرض).\n"
        "• لا تلميح للإجابة الصحيحة.\n\n"
        f"المرحلة: <b>{stage_no}</b> — الأسئلة: <b>{total_q}</b>\n"
        f"💸 سعر المحاولة: <b>{price}</b> ل.س\n"
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

# رسائل نوافذ + مزاح
def _pick_banter(group_key: str, stage_no: int, settings: dict) -> str:
    table = settings.get(group_key, {})
    if not isinstance(table, dict):
        return ""
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
    tpl = settings["ui"].get("windows_error_template") or (
        "🪟 <b>خطأ - Windows</b>\n"
        "<b>الرمز:</b> WRONG_ANSWER\n"
        "<b>الوصف:</b> الخيار غير صحيح أو انتهى الوقت.\n"
        "<b>الإجراء:</b> اضغط «إعادة المحاولة» (سيُخصم {price} ل.س)."
    )
    return tpl.replace("{price}", str(price))

def _windows_success(award_pts: int, total_pts: int, settings: dict) -> str:
    tpl = settings["ui"].get("windows_success_template") or (
        "🪟 <b>Windows - تهانينا</b>\n"
        "<b>الحدث:</b> CORRECT_ANSWER\n"
        "<b>الوصف:</b> إجابة صحيحة! (+{award_pts} نقاط)\n"
        "<b>إجمالي نقاطك:</b> {total_pts}\n"
        "<b>الإجراء:</b> استعد للسؤال التالي 🚀"
    )
    return tpl.replace("{award_pts}", str(award_pts)).replace("{total_pts}", str(total_pts))

# ------------------------ مؤقّت السؤال (تحرير نفس الرسالة) ------------------------
def _start_timer(bot: TeleBot, chat_id: int, msg_id: int, user_id: int, settings: dict):
    # نجلب المرحلة الحالية كي نحسب زمن السؤال حسب المرحلة
    st, _item, stage_no, _ = next_question(user_id)
    total = int(get_seconds_for_stage(stage_no, settings))
    tick  = int(settings.get("timer_tick_seconds", settings["ui"].get("tick_seconds", 1)))

    cancel = threading.Event()
    set_runtime(user_id, timer_cancel=cancel, last_answer_ts=0.0)

    def _loop():
        left = total
        while left > 0 and not cancel.is_set():
            try:
                st, item, stage_no, q_idx = next_question(user_id)
                bal_hint = int(st.get("last_balance", 0))
                txt = _question_text(stage_no, q_idx, item, settings, left, bal_hint=bal_hint)
                kb  = _question_markup(item)
                bot.edit_message_text(txt, chat_id, msg_id, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
            time.sleep(tick)
            left -= tick

        # انتهاء الوقت → عطّل أزرار السؤال وأرسل نافذة خطأ
        if not cancel.is_set():
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception:
                pass
            st, _item, stage_no, _ = next_question(user_id)
            price = get_attempt_price(stage_no, load_settings())
            banter = _pick_banter("banter_wrong_by_stage", stage_no, settings)
            txt = f"💬 {banter}\n{_windows_error(price, settings)}"
            bot.send_message(chat_id, txt, reply_markup=_after_wrong_markup(price), parse_mode="HTML")

    threading.Thread(target=_loop, daemon=True).start()

# ------------------------ شاشة تمهيد ------------------------
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
        # بداية نظيفة
        st = reset_progress(user_id)
        st["stage_stars"] = 0
        st["stage_wrong_attempts"] = 0
        st["stage_done"] = 0
        st["attempts_on_current"] = 0
        st["last_click_ts"] = 0.0
        st.pop("active_msg_id", None)
        st.pop("last_info_msg_id", None)
        user_quiz_state[user_id] = st
        persist_state(user_id)
        _intro_screen(bot, msg.chat.id, user_id)

    # Debounce للنقرات (1s)
    def _click_guard(user_id: int) -> bool:
        st = user_quiz_state.get(user_id, {}) or {}
        now = time.time()
        last = float(st.get("last_click_ts", 0.0))
        if now - last < 1.0:
            return True
        st["last_click_ts"] = now
        user_quiz_state[user_id] = st
        persist_state(user_id)
        return False

    # عرض السؤال (خصم مسبق) — من: quiz_next / quiz_retry / quiz_resume
    def _send_next_question(bot: TeleBot, chat_id: int, user_id: int, delete_msg_ids: list[int] | None = None) -> bool:
        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)

        # أوقف أي مؤقّت سابق بأمان
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass
        clear_runtime(user_id)

        # ✅ خصم السعر قبل الإظهار (آمن ضد التكرار/الاستئناف)
        ok, new_bal, price, reason = ensure_paid_before_show(user_id)
        if not ok:
            bal, _ = get_wallet(user_id)
            bot.send_message(
                chat_id,
                f"❌ رصيدك غير كافٍ لسعر المحاولة.\n"
                f"المطلوب: <b>{price}</b> ل.س — المتاح: <b>{bal}</b> ل.س",
                parse_mode="HTML"
            )
            return False

        st = user_quiz_state.get(user_id, {})  # قد يكون تغيّر داخل ensure_paid_before_show
        st["last_balance"] = new_bal
        user_quiz_state[user_id] = st
        persist_state(user_id)

        # احذف رسالة النتيجة/المقدمة التي ضغط منها
        if delete_msg_ids:
            for mid in delete_msg_ids:
                try: bot.delete_message(chat_id, mid)
                except Exception: pass
        # احذف السؤال القديم إن وُجد
        old_q = st.get("active_msg_id")
        if old_q:
            try: bot.delete_message(chat_id, old_q)
            except Exception: pass

        # أرسل السؤال + عدّاد
        total_secs = int(get_seconds_for_stage(stage_no, settings))
        txt = _question_text(stage_no, q_idx, item, settings, total_secs, bal_hint=new_bal)
        sent = bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=_question_markup(item))

        st["active_msg_id"] = sent.message_id
        st["started_at"]    = int(time.time() * 1000)
        user_quiz_state[user_id] = st
        persist_state(user_id)

        _start_timer(bot, chat_id, sent.message_id, user_id, settings)
        return True

    # الأزرار: التالي/إعادة/متابعة
    @bot.callback_query_handler(func=lambda c: c.data in ("quiz_next", "quiz_retry", "quiz_resume"))
    def on_next_or_retry(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id)
        except: pass
        if _click_guard(user_id):
            return

        delete_ids = [call.message.message_id]
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

        # أوقف المؤقّت
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass
        clear_runtime(user_id)

        settings = load_settings()
        st, item, stage_no, q_idx = next_question(user_id)
        idx = int(call.data.split(":")[1])
        is_correct = (idx == int(item["correct_index"]))
        attempts_on_current = int(st.get("attempts_on_current", 0))

        # احذف رسالة السؤال فورًا
        active_mid = st.get("active_msg_id")
        if active_mid:
            try: bot.delete_message(chat_id, active_mid)
            except Exception:
                try: bot.edit_message_reply_markup(chat_id, active_mid, reply_markup=None)
                except Exception: pass

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
            persist_state(user_id)

            # هل هو آخر سؤال في المرحلة؟
            tpl = load_template(st["template_id"])
            items = tpl.get("items_by_stage", {}).get(str(stage_no), []) or []
            is_last_in_stage = (q_idx == len(items) - 1)

            ok_box = _windows_success(award_pts, pts, settings)
            ok_msg = bot.send_message(
                chat_id,
                ok_box,
                parse_mode="HTML",
                reply_markup=_after_correct_markup()
            )
            st["last_info_msg_id"] = ok_msg.message_id
            user_quiz_state[user_id] = st
            persist_state(user_id)

            # تقدّم المؤشر للسؤال التالي (العرض عند "التالي")
            advance(user_id)

            if is_last_in_stage:
                summary = compute_stage_reward_and_finalize(user_id, stage_no, len(items))
                bot.send_message(
                    chat_id,
                    "🏁 <b>ملخص المرحلة</b>\n"
                    f"المرحلة: <b>{stage_no}</b>\n"
                    f"الأسئلة المنجَزة: <b>{summary['questions']}</b>\n"
                    f"النجوم: <b>{summary['stars']}</b>\n"
                    f"🎁 جائزة المرحلة (نقاط): <b>{summary['reward_points']}</b>\n"
                    f"🏅 نقاطك الآن: <b>{summary['points_after']}</b>",
                    parse_mode="HTML"
                )

        else:
            # خطأ → عدادات المرحلة
            st["stage_wrong_attempts"] = int(st.get("stage_wrong_attempts", 0)) + 1
            st["attempts_on_current"]  = attempts_on_current + 1
            user_quiz_state[user_id] = st
            persist_state(user_id)

            price = get_attempt_price(stage_no, settings)
            banter = _pick_banter("banter_wrong_by_stage", stage_no, settings)
            wrong_msg = bot.send_message(
                chat_id,
                f"💬 {banter}\n{_windows_error(price, settings)}",
                parse_mode="HTML",
                reply_markup=_after_wrong_markup(price)
            )
            st["last_info_msg_id"] = wrong_msg.message_id
            user_quiz_state[user_id] = st
            persist_state(user_id)

    # تحويل النقاط → رصيد
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
        st, _item, stage_no, _ = next_question(call.from_user.id)
        secs = int(get_seconds_for_stage(stage_no, settings))
        price_hint = get_attempt_price(stage_no, settings)
        msg = (
            "ℹ️ <b>شرح اللعبة</b>\n"
            f"• لديك عدّاد وقت: <b>{secs} ثانية</b> للسؤال الحالي.\n"
            "• عند «ابدأ الآن/التالي» أو «إعادة المحاولة» يُخصم ثمن المحاولة فورًا.\n"
            "• لا نعرض أي تلميح للإجابة الصحيحة.\n"
            f"• سعر المحاولة الحالي: {price_hint} ل.س (يتغيّر حسب المرحلة)."
        )
        bot.send_message(call.message.chat.id, msg, parse_mode="HTML")

    # إيقاف مؤقت: رجوع لبداية الزر
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_pause")
    def on_pause(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        try: bot.answer_callback_query(call.id, "تم الحفظ. رجعناك لبداية الزر.")
        except: pass

        # ✅ اجعل السؤال غير مدفوع ليُخصم عند الاستئناف
        pause_current_question(user_id)

        # أوقف المؤقّت الحالي إن وُجد
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass
        clear_runtime(user_id)

        # احذف رسالة النتيجة الأخيرة إن وُجدت
        st = user_quiz_state.get(user_id, {}) or {}
        last_info = st.get("last_info_msg_id")
        if last_info:
            try: bot.delete_message(chat_id, last_info)
            except Exception: pass
            st.pop("last_info_msg_id", None)
            user_quiz_state[user_id] = st
            persist_state(user_id)

        _intro_screen(bot, chat_id, user_id, resume_only=False)

    # إلغاء من الشاشة التمهيدية
    @bot.callback_query_handler(func=lambda c: c.data == "quiz_cancel")
    def on_cancel(call):
        user_id = call.from_user.id
        try: bot.answer_callback_query(call.id, "تم الإلغاء.")
        except: pass
        # أوقف المؤقّت إن وُجد
        rt = get_runtime(user_id)
        cancel = rt.get("timer_cancel")
        if cancel:
            try: cancel.set()
            except: pass
        clear_runtime(user_id)
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
