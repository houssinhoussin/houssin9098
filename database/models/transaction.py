# نموذج المعاملة Transaction Model

from datetime import datetime

class Transaction:
    def __init__(self, user_id, amount, description, timestamp=None):
        self.user_id = user_id
        self.amount = amount
        self.description = description
        self.timestamp = timestamp or datetime.now()

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "amount": self.amount,
            "description": self.description,
            "timestamp": self.timestamp.isoformat()
        }

    def __str__(self):
        return f"💸 معاملة: {self.amount} | {self.description} | {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"


@bot.message_handler(commands=['cancel'])
def cancel_cmd(m):
    try:
        for dct in (globals().get('_msg_by_id_pending', {}),
                    globals().get('_disc_new_user_state', {}),
                    globals().get('_admin_manage_user_state', {}),
                    globals().get('_address_state', {}),
                    globals().get('_phone_state', {})):
            try:
                dct.pop(m.from_user.id, None)
            except Exception:
                pass
    except Exception:
        pass
    try:
        bot.reply_to(m, "✅ تم الإلغاء ورجعناك للقائمة الرئيسية.")
    except Exception:
        bot.send_message(m.chat.id, "✅ تم الإلغاء.")
