from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
import threading

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["http://allowed.com"], allow_methods=["*"], allow_headers=["*"])

@app.websocket("/ws/test")
async def test_ws(websocket: WebSocket):
    await websocket.accept()

def run_server():
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="info")

if __name__ == "__main__":
    t = threading.Thread(target=run_server)
    t.daemon = True
    t.start()
    
    import time
    time.sleep(1)
    
    import websockets
    async def client():
        try:
            async with websockets.connect("ws://127.0.0.1:8002/ws/test", origin="http://notallowed.com") as ws:
                print("Connected!")
        except Exception as e:
            print("Error:", repr(e))
    asyncio.run(client())
