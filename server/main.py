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
from app.search import search_symbols
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


# ─── Search ────────────────────────────────────────────────────────
@app.get("/api/search")
async def api_search(q: str = "", limit: int = 10,
                     _: dict = Depends(get_current_user)):
    """티커/종목명 통합 검색 — 한국어/영어/숫자 모두 지원, 한·미 동시."""
    return await search_symbols(q, limit=limit)


# ─── Watchlist ─────────────────────────────────────────────────────
class WatchIn(BaseModel):
    symbol: str
    capital: float = Field(default=0, ge=0)   # 0이면 투자금 미입력 → 사이징 생략
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


# ─── Analyze 보조: 시장 수급 deterministic 보강 ──────────────────
def _ensure_flow_fields(ana: dict, snap: dict, symbol: str) -> dict:
    """AI 응답에 flow_institutional 등이 빠져있으면 시세/수급 데이터로 채워 넣는다.

    한국시장: KIS의 외국인/기관 순매수 수량 직접 사용 (공식 KRX 데이터)
    미국시장: 거래량(RV) + VWAP 위치로 추론 (다크풀 무료 데이터 부재)
    """
    is_kr = symbol.isdigit() and len(symbol) == 6
    q = snap.get("quote") or {}
    ind = snap.get("indicators") or {}
    rv = float(q.get("relative_volume") or 1.0)
    above_vwap = bool(ind.get("above_vwap"))

    # 이미 필드가 있으면 그대로 둠 (AI가 의미 있게 채운 경우 보존)
    if not ana.get("flow_institutional"):
        if is_kr:
            flow_kr = snap.get("flow_kr") or {}
            foreign = int(flow_kr.get("foreign_net_qty") or 0)
            inst = int(flow_kr.get("institutional_net_qty") or 0)
            retail = int(flow_kr.get("retail_net_qty") or 0)
            net = foreign + inst
            if net > 0:
                ana["flow_institutional"] = "기관 우위"
            elif net < 0:
                ana["flow_institutional"] = "개인 우위"
            else:
                ana["flow_institutional"] = "중립"
            if foreign or inst:
                ana["flow_institutional_reason"] = (
                    f"외국인 {foreign:+,}주, 기관 {inst:+,}주 순매수 (KRX 공식)"
                )
            else:
                ana["flow_institutional_reason"] = "수급 데이터 미수신 (KIS 폴백 모드)"
            ana.setdefault("flow_retail",
                           "매수세" if retail > 0 else "매도세" if retail < 0 else "중립")
        else:
            if rv >= 1.5 and above_vwap:
                ana["flow_institutional"] = "기관 우위"
                ana["flow_institutional_reason"] = (
                    f"거래량 {rv:.2f}x 급증 + VWAP 상회 → 기관 매집 시그널"
                )
            elif rv >= 1.5 and not above_vwap:
                ana["flow_institutional"] = "개인 우위"
                ana["flow_institutional_reason"] = (
                    f"거래량 {rv:.2f}x 급증 + VWAP 하회 → 개인 추격매도/패닉셀 가능"
                )
            else:
                ana["flow_institutional"] = "중립"
                ana["flow_institutional_reason"] = (
                    f"거래량 {rv:.2f}x · VWAP {'상회' if above_vwap else '하회'} (특이 시그널 없음)"
                )
            ana.setdefault("flow_retail",
                           "매수세" if rv >= 1.5 and above_vwap else
                           "매도세" if rv >= 1.5 and not above_vwap else "중립")

    # 특이사항 보강
    if not ana.get("flow_special"):
        notes = []
        if rv >= 2.0:
            notes.append(f"거래량 {rv:.1f}x 폭발")
        elif rv >= 1.5:
            notes.append(f"거래량 {rv:.1f}x 급증")
        if is_kr:
            flow_kr = snap.get("flow_kr") or {}
            f = int(flow_kr.get("foreign_net_qty") or 0)
            if abs(f) >= 100000:
                notes.append(f"외국인 {f:+,}주")
        ana["flow_special"] = " · ".join(notes) if notes else "특이사항 없음"

    return ana


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
        # AI 응답에 시장수급 필드가 빠져있으면 deterministic 계산해서 주입 (항상 표시 보장)
        ana = _ensure_flow_fields(ana, snap, symbol)
        await db.save_plan(symbol, ana)
        # 워치리스트 목록에도 포지션 저장
        await db.update_watch_position(symbol, user["id"], ana["position"], ana["position_emoji"])

        # 투자금이 입력된 경우만 사이징 계산 (capital > 0)
        sizing = None
        if watch and watch.get("capital", 0) > 0 and ana.get("stop_price"):
            s = shares_for(watch["capital"], watch["risk_pct"],
                           float(ana.get("entry_price") or snap["quote"]["price"]),
                           float(ana["stop_price"]))
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
    """실시간 시세와 추천 포지션이 포함된 내 포트폴리오"""
    holdings = await db.list_portfolio(user["id"])
    if not holdings: return []
    
    results = []
    for h in holdings:
        symbol = h["symbol"]
        try:
            # 실시간 시세와 캐시된 분석 결과 가져오기
            snap = await asyncio.to_thread(get_snapshot, symbol)
            plan = await db.get_plan(symbol)
            
            cur_price = snap["quote"]["price"]
            pnl = (cur_price - h["entry_price"]) * h["shares"]
            pnl_pct = (cur_price / h["entry_price"] - 1) * 100 if h["entry_price"] > 0 else 0
            
            results.append({
                **h,
                "current_price": cur_price,
                "change_pct": snap["quote"]["change_pct"],
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "position": plan["position"] if plan else "관망",
                "emoji": plan["position_emoji"] if plan else "⚪"
            })
        except:
            results.append({**h, "current_price": 0, "pnl": 0, "pnl_pct": 0, "position": "Error", "emoji": "❓"})
    
    return results

@app.post("/api/portfolio")
async def api_add_portfolio(data: dict, user: dict = Depends(get_current_user)):
    # data: symbol, entry_price, krw_invested, shares
    await db.add_to_portfolio(user["id"], data["symbol"], data["entry_price"], data["krw_invested"], data.get("shares", 0))
    return {"ok": True}

@app.delete("/api/portfolio/{pid}")
async def api_remove_portfolio(pid: int, user: dict = Depends(get_current_user)):
    await db.remove_from_portfolio(pid, user["id"])
    return {"ok": True}

from app.ocr_portfolio import extract_portfolio_from_image
from fastapi import UploadFile, File

@app.post("/api/portfolio/upload")
async def api_upload_portfolio(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """포트폴리오 스크린샷을 분석하여 일괄 등록"""
    content = await file.read()
    holdings = await asyncio.to_thread(extract_portfolio_from_image, content)
    
    if not holdings:
        return {"ok": False, "msg": "이미지에서 종목 정보를 추출하지 못했습니다."}
    
    count = 0
    for h in holdings:
        try:
            # 기본값 처리 및 정규화
            symbol = str(h.get("symbol", "")).upper()
            price = float(h.get("entry_price", 0))
            krw = float(h.get("krw_invested", 0))
            if symbol and price > 0:
                await db.add_to_portfolio(user["id"], symbol, price, krw)
                count += 1
        except Exception as e:
            log.error(f"보유종목 일괄 등록 중 스킵: {symbol}, {e}")
            
    return {"ok": True, "count": count, "msg": f"{count}개의 종목이 포트폴리오에 등록되었습니다."}


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
