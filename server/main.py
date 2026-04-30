"""FastAPI 진입점 — REST + WebSocket + 알림 워커."""
from __future__ import annotations
import asyncio, json, logging, os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.market import get_snapshot
from app.news import fetch_news, fetch_profile, fetch_market_flow
from app.analyze import analyze
from . import db, alerts as alerts_mod
from .sizing import shares_for, split_plan

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("server")

# ─── WS broadcaster ────────────────────────────────────────────────
_clients: set[WebSocket] = set()


async def broadcast(payload: dict):
    dead = []
    msg = json.dumps(payload, ensure_ascii=False)
    for ws in list(_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


# ─── lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    task = asyncio.create_task(alerts_mod.worker(broadcast))
    log.info("Toss server ready")
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Toss — Quant Assistant", lifespan=lifespan)

STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html")


# ─── REST ──────────────────────────────────────────────────────────
class WatchIn(BaseModel):
    symbol: str
    capital: float = Field(gt=0)
    risk_pct: float = Field(default=1.0, gt=0, le=10)


@app.get("/api/watchlist")
async def api_list():
    return await db.list_watch()


@app.post("/api/watchlist")
async def api_add(item: WatchIn):
    await db.upsert_watch(item.symbol, item.capital, item.risk_pct)
    return {"ok": True}


@app.delete("/api/watchlist/{symbol}")
async def api_del(symbol: str):
    await db.remove_watch(symbol)
    return {"ok": True}


@app.post("/api/analyze/{symbol}")
async def api_analyze(symbol: str):
    symbol = symbol.upper()
    try:
        snap = await asyncio.to_thread(get_snapshot, symbol)
        news, profile, flow = await asyncio.gather(
            fetch_news(symbol), fetch_profile(symbol), fetch_market_flow(symbol)
        )
        ana = await asyncio.to_thread(analyze, symbol, snap, news, flow, profile)
    except Exception as e:
        log.exception("analyze failed")
        raise HTTPException(500, str(e))

    await db.save_plan(symbol, ana)

    # 사이징 미리 계산해서 같이 반환
    watch = next((w for w in await db.list_watch() if w["symbol"] == symbol), None)
    sizing = None
    if watch and ana.get("reentry_or_stop_price"):
        s = shares_for(watch["capital"], watch["risk_pct"],
                       float(ana.get("target_price") or snap["quote"]["price"]),
                       float(ana["reentry_or_stop_price"]))
        sizing = {**s, "splits": split_plan(s["shares"])}

    await broadcast({"type": "analysis", "symbol": symbol})
    return {"snapshot": snap, "analysis": ana,
            "profile": profile, "news": news[:5], "sizing": sizing}


@app.get("/api/alerts")
async def api_alerts():
    return await db.recent_alerts()


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    _clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "hello"}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)


def run():
    import uvicorn
    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
