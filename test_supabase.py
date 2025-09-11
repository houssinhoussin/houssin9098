from database.db import get_table

TABLE_NAME = "USERS_TABLE"
table = get_table(TABLE_NAME)
result = table.select("*").limit(1).execute()
print(result.data)

