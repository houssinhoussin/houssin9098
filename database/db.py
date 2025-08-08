# database/db.py
import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in environment (.env)")

_supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def client() -> Client:
    return _supabase

def get_table(table_name: str):
    return _supabase.table(table_name)
