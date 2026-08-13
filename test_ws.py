import asyncio
import websockets

async def test():
    try:
        async with websockets.connect("ws://localhost:8000/ws/tenants/1/activity", origin="http://localhost:3000") as ws:
            print("Connected with origin localhost:3000!")
            await asyncio.sleep(1)
    except Exception as e:
        print("Error:", repr(e))

    try:
        async with websockets.connect("ws://localhost:8000/ws/tenants/1/activity", origin="http://192.168.1.10:3000") as ws:
            print("Connected with origin 192.168.1.10:3000!")
            await asyncio.sleep(1)
    except Exception as e:
        print("Error:", repr(e))

asyncio.run(test())
