import asyncio
import os
from dotenv import load_dotenv

load_dotenv('.env.development')

from src.db.session import async_sessionmaker, engine
from sqlalchemy import text
import json

async def main():
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        result = await session.execute(text("SELECT id, result FROM jobs WHERE job_type = 'upload_csv' ORDER BY created_at DESC LIMIT 1"))
        row = result.fetchone()
        if row:
            print("Job ID:", row[0])
            res = row[1]
            if res:
                scores = res.get('anomaly_scores', [])
                seq_ts = res.get('sequence_timestamps', [])
                print(f"len scores: {len(scores)}")
                print(f"len seq_ts: {len(seq_ts)}")
                print("First 5 seq_ts:", seq_ts[:5])
        else:
            print("No jobs found")

asyncio.run(main())
