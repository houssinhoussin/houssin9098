# -*- coding: utf-8 -*-
# handlers/cancel.py
from handlers import keyboards

def register(bot, history):
    # نستفيد من دالة التنظيف العامة الموجودة أصلًا
    try:
        from handlers.start import _reset_user_flows
    except Exception:
        _reset_user_flows = None

    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(m):
        user_id = m.from_user.id

        # امسح أي حالات/تدفّقات تخصّ هذا المستخدم
        if callable(_reset_user_flows):
            try:
                _reset_user_flows(user_id)
            except Exception:
                pass

        # مسح تاريخ المستخدم إن كنت تستخدمه للتنقّل
        try:
            history.pop(user_id, None)
        except Exception:
            pass

        # مسح حالات إداريّة إضافية (إن وُجدت) بدون كسر أي شيء
        try:
            from handlers import admin
            for d in (
                getattr(admin, "_accept_pending", None),
                getattr(admin, "_msg_pending", None),
                getattr(admin, "_broadcast_pending", None),
                getattr(admin, "_msg_by_id_pending", None),
                getattr(admin, "_ban_pending", None),
                getattr(admin, "_unban_pending", None),
                getattr(admin, "_cancel_pending", None),
            ):
                if isinstance(d, dict):
                    d.pop(user_id, None)
        except Exception:
            pass

        # رد للمستخدم وإرجاعه للقائمة الرئيسية
        try:
            bot.send_message(
                m.chat.id,
                "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.",
                reply_markup=keyboards.main_menu()
            )
        except Exception:
            bot.send_message(m.chat.id, "✅ تم الإلغاء.")
