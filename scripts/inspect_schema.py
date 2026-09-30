from dotenv import load_dotenv
from sqlalchemy import text
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

load_dotenv()
from database import engine

with engine.connect() as connection:
    tables = connection.execute(text("select table_name from information_schema.tables where table_schema='public' order by table_name")).scalars().all()
    for table in tables:
        count = connection.execute(text(f'select count(*) from "{table}"')).scalar()
        print(f"{table}\t{count}")
