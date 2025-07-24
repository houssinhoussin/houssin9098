# database/db.py
from supabase import create_client, Client

SUPABASE_URL = "https://azortroeejjomqweintc.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF6b3J0cm9lZWpqb21xd2VpbnRjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTIxOTIzNjUsImV4cCI6MjA2Nzc2ODM2NX0.x3Pwq8OyRmlr7JQuEU2xRxYJtSoz67eIVzDx8Nh4muk"

client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_table(table_name):
    return client.table(table_name)
