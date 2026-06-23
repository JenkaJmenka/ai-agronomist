import asyncio
from src.db.session import AsyncSessionLocal
from src.db.seeds import seed_test_data

async def run():
    async with AsyncSessionLocal() as session:
        await seed_test_data(session)
        print("Test data seeded successfully!")

if __name__ == "__main__":
    asyncio.run(run())
