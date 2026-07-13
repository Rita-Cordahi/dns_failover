import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.database import PrimarySessionLocal, FallbackSessionLocal, FailoverLog, init_db
from sqlalchemy import select, text

async def main():
    await init_db()
    
    # Insert dummy fallback log
    async with FallbackSessionLocal() as fallback_db:
        log = FailoverLog(event_type="DB_FALLBACK_ACTIVE", message="Simulated outage")
        fallback_db.add(log)
        await fallback_db.commit()
        print("Inserted fallback log.")

    # Execute sync once with exception tracing
    try:
        async with PrimarySessionLocal() as primary_db:
            await primary_db.execute(text("SELECT 1"))
            print("Primary reachable.")

        async with FallbackSessionLocal() as fallback_db:
            stmt = select(FailoverLog).filter(FailoverLog.event_type != "SYNCED").order_by(FailoverLog.id).limit(100)
            res = await fallback_db.execute(stmt)
            pending = res.scalars().all()
            print(f"Pending logs in fallback: {len(pending)}")
            
            synced_logs = []
            async with PrimarySessionLocal() as sync_db:
                for log in pending:
                    synced = FailoverLog(
                        event_type=log.event_type,
                        message=log.message,
                        timestamp=log.timestamp,
                    )
                    sync_db.add(synced)
                    synced_logs.append(synced)
                await sync_db.commit()
                print("Committed to primary.")

            # Mark synced in fallback
            for log in pending:
                log.event_type = "SYNCED"
            await fallback_db.commit()
            print("Marked synced in fallback.")
            
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
