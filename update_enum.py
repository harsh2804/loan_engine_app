import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://postgres:password1234@localhost:5432/loan_engine')
    try:
        await conn.execute("ALTER TYPE application_status ADD VALUE 'AA_CONSENT_COMPLETED' AFTER 'AA_CONSENT_GIVEN';")
        print('Enum updated successfully.')
    except Exception as e:
        print('Error:', e)
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
