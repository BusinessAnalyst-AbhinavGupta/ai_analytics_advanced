from fastapi import FastAPI, WebSocket
import uvicorn
import asyncio
import threading

app = FastAPI()

def run_server():
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="info")

if __name__ == "__main__":
    t = threading.Thread(target=run_server)
    t.daemon = True
    t.start()
    
    import time
    time.sleep(1)
    
    import websockets
    async def client():
        try:
            async with websockets.connect("ws://127.0.0.1:8003/ws/test") as ws:
                print("Connected!")
        except Exception as e:
            pass
    asyncio.run(client())
