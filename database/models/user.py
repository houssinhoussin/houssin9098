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
