# نموذج المستخدم User Model

class User:
    def __init__(self, user_id, username=None, name=None, balance=0):
        self.user_id = user_id
        self.username = username
        self.name = name
        self.balance = balance

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "username": self.username,
            "name": self.name,
            "balance": self.balance
        }

    def __str__(self):
        return f"👤 المستخدم: {self.name or self.username} | الرصيد: {self.balance}"


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
