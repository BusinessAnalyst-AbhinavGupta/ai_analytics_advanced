from fastapi import FastAPI, Request
import uvicorn
import asyncio
from httpx import AsyncClient

app = FastAPI()

@app.middleware("http")
async def _access_log_middleware(request: Request, call_next):
    response = await call_next(request)
    route = request.scope.get("route")
    route_name = route.name if route else "unknown"
    print(f"Path: {request.url.path}, Route Name: {route_name}, Endpoint: {request.scope.get('endpoint')}")
    return response

@app.get("/hello/{name}")
def hello(name: str):
    return {"hello": name}

async def test():
    config = uvicorn.Config(app=app, host="127.0.0.1", port=8002)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(1)
    async with AsyncClient(base_url="http://127.0.0.1:8002") as client:
        await client.get("/hello/world")
    server.should_exit = True
    await task

if __name__ == "__main__":
    asyncio.run(test())
