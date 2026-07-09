import asyncio
import os
import subprocess
import sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def reset_db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("❌ ERROR: Please set DATABASE_URL")
        print("Example: export DATABASE_URL='postgresql+asyncpg://postgres:password@containers-us-west...railway.app:5432/railway'")
        sys.exit(1)
        
    # Standardize the connection URL for asyncpg
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://")
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
        
    print(f"🔌 Connecting to remote database...")
    engine = create_async_engine(url)
    
    try:
        async with engine.begin() as conn:
            print("🗑️ Dropping all tables (DROP SCHEMA public CASCADE)...")
            await conn.execute(text("DROP SCHEMA public CASCADE;"))
            await conn.execute(text("CREATE SCHEMA public;"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO public;"))
            await conn.execute(text("GRANT ALL ON SCHEMA public TO postgres;"))
    except Exception as e:
        print(f"❌ Failed to drop schema: {e}")
        sys.exit(1)
    finally:
        await engine.dispose()
        
    print("✅ Database wiped successfully.\n")
    print("🏗️ Running Alembic migrations...")
    
    # Execute alembic using the current python executable
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    except subprocess.CalledProcessError:
        print("❌ Migrations failed!")
        sys.exit(1)
        
    print("✅ Migrations applied successfully.\n")
    print("🌱 Running database seeds...")
    
    # Run the seed script
    try:
        subprocess.run([sys.executable, "run_seeds.py"], check=True)
    except subprocess.CalledProcessError:
        print("❌ Seeding failed!")
        sys.exit(1)
        
    print("✨ All done! The remote database is completely fresh.")

if __name__ == "__main__":
    asyncio.run(reset_db())
