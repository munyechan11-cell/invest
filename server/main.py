import sys, os, asyncio, json, logging
from pathlib import Path
from contextlib import asynccontextmanager

# 경로 설정
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

# 프로젝트 모듈 임포트 (가장 안전한 방식)
try:
    from app.market import get_snapshot
    from app.news import fetch_news, fetch_profile, fetch_market_flow
    from app.analyze import analyze
    from server import db, alerts as alerts_mod
    from server.sizing import shares_for, split_plan
except ImportError as e:
    # 패키지 구조 문제 발생 시 상대 경로로 시도
    from . import db, alerts as alerts_mod
    from .sizing import shares_for, split_plan
    from app.market import get_snapshot
    from app.news import fetch_news, fetch_profile, fetch_market_flow
    from app.analyze import analyze

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")
log = logging.getLogger("server")
security = HTTPBearer()

_clients: set[WebSocket] = set()

async def broadcast(payload: dict):
    msg = json.dumps(payload, ensure_ascii=False)
    for ws in list(_clients):
        try:
            await ws.send_text(msg)
        except:
            _clients.discard(ws)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB 초기화 및 알림 워커 시작
    await db.init()
    task = asyncio.create_task(alerts_mod.worker(broadcast))
    log.info("Toss Server LifeSpan Started")
    yield
    task.cancel()

app = FastAPI(title="Invest Quant", lifespan=lifespan)
STATIC = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html")

# --- 인증 및 분석 API (기존 로직 유지하되 안전하게 포장) ---
# (중략 - 중복을 피하기 위해 핵심 로직만 확실히 재배치)

@app.post("/api/analyze/{symbol}")
async def api_analyze(symbol: str, user: dict = Depends(db.get_current_user if hasattr(db, 'get_current_user') else None)):
    # ... (생략된 분석 로직은 기존과 동일하나 함수 호출 경로만 db. 활용) ...
    # 이 부분은 실제 파일에서 api_analyze 함수 전체를 유지합니다.
    pass

# (이후 모든 API 엔드포인트는 기존의 db.get_current_user 등을 사용하여 연결)

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
    
    # 1. 캐시 확인 (10분 이내 데이터가 있으면 AI 호출 생략하여 429 방지)
    cached = await db.get_plan(symbol, max_age_sec=600)
    if cached:
        log.info(f"Using cached analysis for {symbol}")
        # 시세는 최신 정보를 위해 새로 가져옴
        snap = await asyncio.to_thread(get_snapshot, symbol)
        await broadcast({"type": "analysis", "symbol": symbol, "user_id": user["id"]})
        return {"snapshot": snap, "analysis": cached, "cached": True}

    try:
        # 2. 병렬 데이터 수집 (속도 최적화: 4가지 소스를 동시에 호출)
        log.info(f"Fetching data in parallel for {symbol}")
        snap_task = asyncio.to_thread(get_snapshot, symbol)
        news_task = fetch_news(symbol) # app.news에서 가져온 async 함수
        profile_task = fetch_profile(symbol) # app.news에서 가져온 async 함수
        flow_task = fetch_market_flow(symbol) # app.news에서 가져온 async 함수
        
        # get_snapshot은 동기 함수이므로 to_thread 활용, 나머지는 async
        snap, news, profile, flow = await asyncio.gather(
            snap_task, news_task, profile_task, flow_task
        )
        
        # 3. AI 분석 수행 (동기 함수이므로 thread에서 실행)
        ana = await asyncio.to_thread(analyze, symbol, snap, news, flow, profile)
        
        # 결과 저장 (캐싱)
        await db.save_plan(symbol, ana)

        # 사이징 미리 계산
        watch = next((w for w in await db.list_watch(user["id"]) if w["symbol"] == symbol), None)
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
        # 429 에러 발생 시 사용자에게 친절한 안내
        if "429" in str(e):
            raise HTTPException(429, "분석 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요 (캐시가 생성될 예정입니다).")
        raise HTTPException(500, f"분석 중 오류 발생: {str(e)}")


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
    # Render는 PORT 환경변수를 통해 포트를 지정해줍니다. 기본값은 8000.
    port = int(os.environ.get("PORT", 8000))
    # 127.0.0.1이 아닌 0.0.0.0으로 열어야 외부에서 접속이 가능합니다.
    uvicorn.run("server.main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    run()
