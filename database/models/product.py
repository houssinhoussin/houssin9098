# نموذج المنتج Product Model

class Product:
    def __init__(self, product_id, name, category, price, description=None, code=None, stock=1):
        self.product_id = product_id
        self.name = name
        self.category = category  # مثلاً: "ألعاب", "كاش", "بطاقات"
        self.price = price
        self.description = description or ""
        self.code = code or ""
        self.stock = stock

    def to_dict(self):
        return {
            "id": self.product_id,
            "name": self.name,
            "category": self.category,
            "price": self.price,
            "description": self.description,
            "code": self.code,
            "stock": self.stock
        }

    def __str__(self):
        return f"📦 {self.name} ({self.category}) - {self.price} ل.س"
