from fastapi import FastAPI, WebSocket, HTTPException
import uvicorn
import asyncio
import threading

app = FastAPI()

@app.websocket("/ws/test")
async def test_ws(websocket: WebSocket):
    raise HTTPException(status_code=403, detail="Not allowed")
    await websocket.accept()

def run_server():
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")

if __name__ == "__main__":
    t = threading.Thread(target=run_server)
    t.daemon = True
    t.start()
    
    import time
    time.sleep(1)
    
    import websockets
    async def client():
        try:
            async with websockets.connect("ws://127.0.0.1:8001/ws/test") as ws:
                print("Connected!")
        except Exception as e:
            print("Error:", repr(e))
    asyncio.run(client())
