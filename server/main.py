"""FastAPI 진입점 — REST + WebSocket + 알림 워커."""
from __future__ import annotations
import asyncio, json, logging, os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.market import get_snapshot
from app.news import fetch_news, fetch_profile, fetch_market_flow
from app.analyze import analyze
from . import db, alerts as alerts_mod
from .sizing import shares_for, split_plan

security = HTTPBearer()

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


# ─── Auth ──────────────────────────────────────────────────────────
class AuthIn(BaseModel):
    username: str
    password: str
    display_name: str = ""


class CheckIn(BaseModel):
    username: str


@app.post("/api/check-username")
async def api_check(body: CheckIn):
    taken = await db.check_username(body.username)
    return {"taken": taken}


@app.post("/api/register")
async def api_register(body: AuthIn):
    result = await db.register(body.username, body.password, body.display_name)
    if not result:
        raise HTTPException(409, "이미 존재하는 아이디입니다.")
    return result


@app.post("/api/login")
async def api_login(body: AuthIn):
    result = await db.login(body.username, body.password)
    if not result:
        raise HTTPException(401, "아이디 또는 비밀번호가 틀렸습니다.")
    return result


# ─── Auth Dependency ───────────────────────────────────────────────
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = db.decode_token(token)
    if not payload:
        raise HTTPException(401, "유효하지 않거나 만료된 토큰입니다.")
    uid = int(payload.get("sub", 0))
    user = await db.get_user_by_id(uid)
    if not user:
        raise HTTPException(401, "사용자를 찾을 수 없습니다.")
    return user


async def check_admin(user: dict = Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "관리자 권한이 없습니다.")
    return user


# ─── Admin ─────────────────────────────────────────────────────────
@app.get("/api/admin/users")
async def api_admin_users(admin: dict = Depends(check_admin)):
    return await db.list_users()


@app.delete("/api/admin/users/{user_id}")
async def api_admin_del_user(user_id: int, admin: dict = Depends(check_admin)):
    await db.delete_user(user_id)
    return {"ok": True}


# ─── REST ──────────────────────────────────────────────────────────
class WatchIn(BaseModel):
    symbol: str
    capital: float = Field(gt=0)
    risk_pct: float = Field(default=1.0, gt=0, le=100)
    user_id: int = 0


@app.get("/api/watchlist")
async def api_list(user: dict = Depends(get_current_user)):
    return await db.list_watch(user["id"])


@app.post("/api/watchlist")
async def api_add(item: WatchIn, user: dict = Depends(get_current_user)):
    await db.upsert_watch(item.symbol, item.capital, item.risk_pct, user["id"])
    return {"ok": True}


@app.delete("/api/watchlist/{symbol}")
async def api_del(symbol: str, user: dict = Depends(get_current_user)):
    await db.remove_watch(symbol, user["id"])
    return {"ok": True}


@app.post("/api/analyze/{symbol}")
async def api_analyze(symbol: str, user: dict = Depends(get_current_user)):
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
    watch = next((w for w in await db.list_watch(user["id"]) if w["symbol"] == symbol), None)
    sizing = None
    if watch and ana.get("reentry_or_stop_price"):
        s = shares_for(watch["capital"], watch["risk_pct"],
                       float(ana.get("target_price") or snap["quote"]["price"]),
                       float(ana["reentry_or_stop_price"]))
        sizing = {**s, "splits": split_plan(s["shares"])}

    await broadcast({"type": "analysis", "symbol": symbol, "user_id": user["id"]})
    return {"snapshot": snap, "analysis": ana,
            "profile": profile, "news": news[:5], "sizing": sizing}


# ─── 매매기록 ──────────────────────────────────────────────────────
class TradeIn(BaseModel):
    user_id: int
    symbol: str
    trade_type: str  # BUY, 익절, 손절, 청산
    shares: float
    price: float
    note: str = ""


@app.post("/api/trades")
async def api_add_trade(body: TradeIn, user: dict = Depends(get_current_user)):
    tid = await db.add_trade(user["id"], body.symbol, body.trade_type,
                             body.shares, body.price, body.note)
    return {"ok": True, "trade_id": tid}


@app.get("/api/trades")
async def api_list_trades(user: dict = Depends(get_current_user)):
    return await db.list_trades(user["id"])


@app.get("/api/portfolio")
async def api_portfolio(user: dict = Depends(get_current_user)):
    return await db.portfolio_summary(user["id"])


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
