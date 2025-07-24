from database.db import get_table

TABLE_NAME = "houssin363"
table = get_table(TABLE_NAME)
result = table.select("*").limit(1).execute()
print(result.data)
