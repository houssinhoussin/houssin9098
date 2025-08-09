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
