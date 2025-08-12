# services/idempotency.py
from uuid import uuid4
from services.state_service import get_var, set_var

def _lock_key(flow: str) -> str:
    return f"lock:{flow}"

def _idemp_key(flow: str) -> str:
    return f"idemp:{flow}"

def start_confirm(bot, call, user_id: int, flow: str, lock_ttl_sec: int = 45):
    """
    - يزيل أزرار الرسالة.
    - يمنع الضغطات المتزامنة (lock).
    - يضمن وجود UUID ثابت للطلب.
    يرجّع dict فيه idemp واسم مفتاح القفل. يرجّع None لو فيه تنفيذ قائم.
    """
    # 1) تعطيل الأزرار مباشرة
    try: bot.answer_callback_query(call.id, "⏳ جاري المعالجة…")
    except: pass
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass

    # 2) قفل
    lk = _lock_key(flow)
    if get_var(user_id, lk):   # فيه تنفيذ جارٍ
        try: bot.answer_callback_query(call.id, "قيد التنفيذ… لحظة 🙏", show_alert=False)
        except: pass
        return None
    set_var(user_id, lk, True)

    # 3) UUID ثابت
    ik = _idemp_key(flow)
    kid = get_var(user_id, ik)
    if not kid:
        kid = str(uuid4())
        set_var(user_id, ik, kid)

    return {"lock_key": lk, "idemp_key": kid}

def finish_confirm(bot, call, user_id: int, flow: str, final_text: str | None = None):
    """يفك القفل ويحدّث الرسالة/يبعث رسالة بديلة."""
    set_var(user_id, _lock_key(flow), False)
    if final_text:
        try:
            bot.edit_message_text(final_text, call.message.chat.id, call.message.message_id)
        except:
            try:
                bot.send_message(user_id, final_text)
            except:
                pass

def get_idemp_key(user_id: int, flow: str) -> str | None:
    return get_var(user_id, _idemp_key(flow))
