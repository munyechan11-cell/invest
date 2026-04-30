"""FastAPI 진입점 — REST + WebSocket + 알림 워커."""
from __future__ import annotations
import sys, os, asyncio, json, logging
from pathlib import Path
from contextlib import asynccontextmanager

# 경로 설정 — 모듈 임포트 안정화
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.market import get_snapshot
from app.news import fetch_news, fetch_profile, fetch_market_flow
from app.analyze import analyze
from server import db, alerts as alerts_mod
from server.sizing import shares_for, split_plan

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("server")
security = HTTPBearer()

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
async def lifespan(_app: FastAPI):
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
async def api_admin_users(_: dict = Depends(check_admin)):
    return await db.list_users()


@app.delete("/api/admin/users/{user_id}")
async def api_admin_del_user(user_id: int, _: dict = Depends(check_admin)):
    await db.delete_user(user_id)
    return {"ok": True}


# ─── Watchlist ─────────────────────────────────────────────────────
class WatchIn(BaseModel):
    symbol: str
    capital: float = Field(gt=0)
    risk_pct: float = Field(default=1.0, gt=0, le=100)


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


# ─── Analyze ───────────────────────────────────────────────────────
@app.post("/api/analyze/{symbol}")
async def api_analyze(symbol: str, user: dict = Depends(get_current_user)):
    symbol = symbol.upper()

    # 1. 캐시 확인 (10분 이내 데이터가 있으면 AI 호출 생략하여 429 방지)
    cached = await db.get_plan(symbol, max_age_sec=600)
    if cached:
        log.info(f"Using cached analysis for {symbol}")
        snap = await asyncio.to_thread(get_snapshot, symbol)
        await broadcast({"type": "analysis", "symbol": symbol, "user_id": user["id"]})
        return {"snapshot": snap, "analysis": cached, "cached": True}

    try:
        log.info(f"Fetching data in parallel for {symbol}")
        snap_task = asyncio.to_thread(get_snapshot, symbol)
        news_task = fetch_news(symbol)
        profile_task = fetch_profile(symbol)
        flow_task = fetch_market_flow(symbol)

        snap, news, profile, flow = await asyncio.gather(
            snap_task, news_task, profile_task, flow_task
        )

        watch = next((w for w in await db.list_watch(user["id"]) if w["symbol"] == symbol), None)
        risk_val = watch["risk_pct"] if watch else 1.0

        ana = await asyncio.to_thread(analyze, symbol, snap, news, flow, profile, risk_val)
        await db.save_plan(symbol, ana)
        # 워치리스트 목록에도 포지션 저장
        await db.update_watch_position(symbol, user["id"], ana["position"], ana["position_emoji"])

        sizing = None
        if watch and ana.get("reentry_or_stop_price"):
            s = shares_for(watch["capital"], watch["risk_pct"],
                           float(ana.get("target_price") or snap["quote"]["price"]),
                           float(ana["reentry_or_stop_price"]))
            sizing = {**s, "splits": split_plan(s["shares"])}

        await broadcast({"type": "analysis", "symbol": symbol, "user_id": user["id"]})
        return {"snapshot": snap, "analysis": ana,
                "profile": profile, "news": news[:5], "sizing": sizing, "cached": False}

    except Exception as e:
        log.error(f"Analyze error for {symbol}: {e}")
        # 데이터 수집 실패는 진짜 에러이므로 그대로 던짐.
        # AI 실패는 analyze() 내부에서 룰 기반으로 자동 폴백되므로 여기까진 안 옴.
        raise HTTPException(500, f"데이터 수집 실패: {str(e)}")


# ─── Trades ────────────────────────────────────────────────────────
class TradeIn(BaseModel):
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
async def api_alerts(_: dict = Depends(get_current_user)):
    return await db.recent_alerts()


@app.get("/api/recommendations")
async def api_recommendations():
    """주요 종목 중 실시간 점수가 높은 TOP 5 추천"""
    # 스캔 대상: 주요 미주 우량주
    targets = ["NVDA", "TSLA", "AAPL", "COST", "PLTR", "MSFT", "AMZN", "GOOGL", "META", "AMD"]
    results = []
    
    async def scan(sym):
        try:
            # get_snapshot은 동기 함수이므로 to_thread 사용
            snap = await asyncio.to_thread(get_snapshot, sym)
            from app.analyze_rules import analyze_rules
            # 수급/뉴스는 빈값으로 퀵 스캔 (기술지표 위주)
            ana = analyze_rules(sym, snap, [], {}, {})
            return {
                "symbol": sym, 
                "price": snap["quote"]["price"], 
                "change_pct": snap["quote"]["change_pct"],
                "position": ana["position"], 
                "emoji": ana["position_emoji"],
                "score": ana.get("confidence", 0)
            }
        except Exception:
            return None

    tasks = [scan(s) for s in targets]
    raw = await asyncio.gather(*tasks)
    # '매수' 포지션인 종목만 필터링 (🟢 이모지 포함 여부로 판단)
    results = [r for r in raw if r and "매수" in r["position"]]
    
    # 점수(confidence) 높은 순으로 정렬하여 상위 5개 반환
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:5]


# ─── WebSocket ─────────────────────────────────────────────────────
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
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run("server.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
