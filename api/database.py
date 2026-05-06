import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def get_db_pool():
    return await asyncpg.create_pool(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 5432)),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )