# -*- coding: utf-8 -*-
# database/models/product.py — تخزين السعر كوحدة صغرى (سنت/قرش) بدل float

class Product:
    def __init__(self, product_id, name, category, price=None, description=None, code=None, stock=1, price_cents=None):
        self.product_id = product_id
        self.name = name
        self.category = category
        # نخزن بالسنت/القرش دائمًا
        if price_cents is not None:
            self._price_cents = int(price_cents)
        else:
            # دعم قديم: لو جاء float بالدولار -> حوّل لسنت مرة واحدة
            if price is None:
                self._price_cents = 0
            else:
                try:
                    self._price_cents = int(round(float(price) * 100))
                except Exception:
                    self._price_cents = int(price)
        self.description = description or ""
        self.code = code or ""
        self.stock = stock

    @property
    def price_cents(self) -> int:
        return int(self._price_cents)

    @property
    def price(self) -> float:
        """سعر بالدولار كقيمة عرض فقط (من السنت).""" 
        return self._price_cents / 100.0

    def to_dict(self):
        return {
            "id": self.product_id,
            "name": self.name,
            "category": self.category,
            "price_cents": int(self._price_cents),
            "price": self.price,  # للعرض/التوافق
            "description": self.description,
            "code": self.code,
            "stock": self.stock,
        }


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
