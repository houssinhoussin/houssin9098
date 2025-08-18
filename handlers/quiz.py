
from __future__ import annotations
import threading, time
from telebot import TeleBot, types

from services.quiz_service import (
    load_settings, ensure_user_wallet, get_wallet,
    get_attempt_price, get_stage_time,
    reset_progress, next_question, advance_after_correct, register_wrong_attempt,
    user_quiz_state, load_template, ensure_paid_before_show, mark_seen_after_payment,
    get_leaderboard_top, seen_clear_user, convert_points_to_balance
)

_RUNTIME = {}
def _rt(uid: int) -> dict: return _RUNTIME.setdefault(uid, {})
def _rt_clear(uid: int): _RUNTIME.pop(uid, None)

def start_handlers(bot: TeleBot):

    @bot.callback_query_handler(func=lambda c: c.data.startswith('ans:'))
    def on_answer(cb):
        uid = cb.from_user.id
        ev = _rt(uid).get("tstop")
        if ev: ev.set()  # أوقف المؤقّت
        try:
            bot.answer_callback_query(cb.id)
        except Exception:
            pass
        try:
            chosen = int(cb.data.split(':',1)[1])
        except Exception:
            chosen = -1
        item = _rt(uid).get("cur_item") or {}
        correct = int(item.get("answer", -999))
        if chosen == correct:
            status, data = advance_after_correct(uid)
            if status == "stage_completed":
                bot.edit_message_text("✅ صح! ⭐️ أنهيت مرحلة. سيتم صرف جائزة المرحلة الآمنة الآن.", cb.message.chat.id, cb.message.message_id)
                bot.send_message(cb.message.chat.id, f"💰 جائزة المرحلة: {int(data.get('reward_syp',0))} ل.س", reply_markup=_mk_after_correct())
            elif status == "template_completed":
                bot.edit_message_text("✅ صح! 🥇 مبروك ختم الملف.", cb.message.chat.id, cb.message.message_id)
                bot.send_message(cb.message.chat.id, f"💰 جائزة الختم: {int(data.get('award_syp',0))} ل.س", reply_markup=_mk_after_correct())
            else:
                pts = int(data.get("points_gained", 0))
                pts_txt = f" (+{pts} نقاط)" if pts else ""
                bot.edit_message_text(f"✅ صح!{pts_txt} ⏭️ لننتقل للسؤال التالي.", cb.message.chat.id, cb.message.message_id, reply_markup=None)
                _present_question(bot, cb.message.chat.id, uid, is_retry=False)
        else:
            register_wrong_attempt(uid)
            bot.edit_message_text("❌ خطأ — جرّب من جديد", cb.message.chat.id, cb.message.message_id)
            bot.send_message(cb.message.chat.id, "اختر:", reply_markup=_mk_after_wrong())


    @bot.message_handler(commands=['quiz','start_quiz'])
    def cmd_quiz(msg):
        uid = msg.from_user.id
        ensure_user_wallet(uid, msg.from_user.first_name or str(uid))
        # بداية جديدة تمسح المشاهد
        seen_clear_user(uid)
        reset_progress(uid)
        bot.send_message(msg.chat.id, "🎮 لنبدأ اللعبة من جديد!", reply_markup=_mk_main())

    @bot.callback_query_handler(func=lambda c: c.data in ['start','next','retry','lb','convert_points'])
    def on_cb(cb):
        uid = cb.from_user.id
        if cb.data == 'lb':
            _show_lb(bot, cb.message.chat.id)
            return
        if cb.data == 'convert_points':
            pts, gained = convert_points_to_balance(uid, all_points=True)
            if pts:
                bot.answer_callback_query(cb.id, f"حوّلنا {pts} نقطة → {int(gained)} ل.س")
            else:
                bot.answer_callback_query(cb.id, f"لا تملك نقاطًا لتحويلها.")
            _present_question(bot, cb.message.chat.id, uid, is_retry=False)
            return
        _present_question(bot, cb.message.chat.id, uid, is_retry=(cb.data=='retry'))

    @bot.message_handler(commands=['convert','points'])
    def cmd_convert(msg):
        uid = msg.from_user.id
        pts, gained = convert_points_to_balance(uid, all_points=True)
        if pts:
            bot.reply_to(msg, f"💱 تم تحويل {pts} نقطة إلى {int(gained)} ل.س. رصيدك الآن {int(get_wallet(uid).get('balance',0))} ل.س")
        else:
            bot.reply_to(msg, "لا تملك نقاطًا لتحويلها.")

def _mk_main():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🚀 ابدأ", callback_data='start'),
           types.InlineKeyboardButton("🏆 المتصدرون", callback_data='lb'))
    kb.add(types.InlineKeyboardButton("💱 تحويل النقاط الآن", callback_data='convert_points'))
    return kb

def _mk_after_wrong():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔁 جرّب سؤالًا آخر", callback_data='retry'),
           types.InlineKeyboardButton("🏆 المتصدرون", callback_data='lb'))
    kb.add(types.InlineKeyboardButton("💱 تحويل النقاط الآن", callback_data='convert_points'))
    return kb

def _mk_after_correct():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⏭️ التالي", callback_data='next'),
           types.InlineKeyboardButton("🏆 المتصدرون", callback_data='lb'))
    kb.add(types.InlineKeyboardButton("💱 تحويل النقاط الآن", callback_data='convert_points'))
    return kb

def _show_lb(bot: TeleBot, chat_id: int):
    top = get_leaderboard_top(10)
    if not top:
        bot.send_message(chat_id, "لا يوجد متصدرون بعد.")
        return
    txt = "🏆 <b>المتصدرون</b>\n" + "\n".join([f"{i+1}. {r.get('name','')}: {int(r.get('balance',0))} ل.س" for i,r in enumerate(top)])
    bot.send_message(chat_id, txt, parse_mode='HTML')

def _present_question(bot: TeleBot, chat_id: int, uid: int, is_retry: bool):
    item, stage_no, idx = next_question(uid)
    if idx < 0:
        # اعتبر المرحلة منتهية
        status, data = advance_after_correct(uid)
        if status == "stage_completed":
            bot.send_message(chat_id, f"⭐️ أنهيت مرحلة. جائزة المرحلة: {int(data.get('reward_syp',0))} ل.س", reply_markup=_mk_after_correct())
        elif status == "template_completed":
            bot.send_message(chat_id, f"🥇 مبروك ختم الملف! جائزة الختم: {int(data.get('award_syp',0))} ل.س", reply_markup=_mk_after_correct())
        else:
            bot.send_message(chat_id, "⏭️ نتابع!", reply_markup=_mk_after_correct())
        return

    ok, reason = ensure_paid_before_show(uid, stage_no)
    if not ok:
        bot.send_message(chat_id, reason, reply_markup=_mk_main())
        return

    mark_seen_after_payment(uid, item)

    # عرض السؤال + مؤقّت
    txt = _render_q(uid, stage_no, idx, item)
    _rt(uid)['cur_item']=item
    msg = bot.send_message(chat_id, txt, parse_mode='HTML', reply_markup=_mk_answer_kb(item))

    # مؤقّت
    _start_timer(bot, uid, chat_id, msg, stage_no, item)

def _start_timer(bot: TeleBot, uid: int, chat_id: int, msg, stage_no: int, item: dict):
    sec = get_stage_time(stage_no)
    ev = threading.Event()
    _rt(uid)["tstop"] = ev

    def tick():
        remain = sec
        while remain>0 and not ev.is_set():
            time.sleep(1)
            remain -= 1
            try:
                bot.edit_message_text(_render_q(uid, stage_no, user_quiz_state(uid)["q_index"], item, remain), chat_id, msg.message_id, parse_mode='HTML', reply_markup=_mk_answer_kb(item))
            except Exception:
                pass
        if not ev.is_set() and remain<=0:
            register_wrong_attempt(uid)
            try: bot.edit_message_reply_markup(chat_id, msg.message_id, reply_markup=None)
            except: pass
            bot.send_message(chat_id, "⏱️ خلص الوقت — جرّب من جديد", reply_markup=_mk_after_wrong())

    threading.Thread(target=tick, daemon=True).start()

def _mk_answer_kb(item: dict):
    kb = types.InlineKeyboardMarkup()
    for i,opt in enumerate(item.get("options",[])):
        kb.add(types.InlineKeyboardButton(opt, callback_data=f"ans:{i}"))
    return kb

def _render_q(uid: int, stage_no: int, q_idx: int, item: dict, sec: int=None) -> str:
    if sec is None: sec = get_stage_time(stage_no)
    w = get_wallet(uid)
    bar = "▪" * max(0, sec//5)
    return (f"🎯 <b>المرحلة {stage_no}</b> — السؤال <b>{q_idx+1}</b>\n"
            f"⏱️ {sec:02d}s {bar} — الرصيد {int(w.get('balance',0))} ل.س — السعر {get_attempt_price(stage_no)} ل.س\n\n"
            f"{item.get('text','')}")
# --- Backward compatibility for main.py ---
from telebot import TeleBot as _TB
def attach_handlers(bot: _TB):
    # alias حتى يبقى الاستيراد القديم يعمل
    start_handlers(bot)
