from database.db import get_table

from config import SUPABASE_TABLE_NAME as TABLE_NAME
if TABLE_NAME == "USERS_TABLE":
    TABLE_NAME = "houssin363"
table = get_table(TABLE_NAME)
result = table.select("*").limit(1).execute()
print(result.data)
