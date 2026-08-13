import asyncio
import websockets

async def test():
    try:
        async with websockets.connect(
            "ws://localhost:8000/ws/tenants/1/activity",
            origin="http://localhost:3000",
            extra_headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-US,en;q=0.9",
            }
        ) as ws:
            print("Connected!")
            await asyncio.sleep(1)
    except Exception as e:
        print("Error:", repr(e))

asyncio.run(test())
