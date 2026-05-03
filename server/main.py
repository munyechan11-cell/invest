"""FastAPI 진입점 — REST + WebSocket + 알림 워커."""
from __future__ import annotations
import sys, os, asyncio, json, logging, time
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
from app.intelligence import (
    compute_toss_score, explain_move, compute_multi_tf, detect_patterns,
    compute_relative_strength, get_benchmark_symbol,
)
from app.trade_kis import auto_order, is_live as kis_is_live
from app import telegram_alert, morning_brief, dart_watcher
from app.market_hours import market_status_for
from app.backtest import backtest as run_backtest
from app.scanner import get_top_picks
from app.indices import fetch_indices
from app.risk_analytics import analyze_portfolio_risk, grade_volatility
from server import db, alerts as alerts_mod
from server.sizing import shares_for, split_plan

# 스냅샷 인메모리 캐시 — AI 분석은 10분 캐시지만 시세는 더 자주 새로
_snapshot_cache: dict[str, tuple[float, dict]] = {}
_SNAPSHOT_TTL = 8  # 초


async def _cached_snapshot(symbol: str) -> dict:
    """스냅샷 인메모리 캐시 — 동시 요청 시 중복 호출 방지."""
    import time as _t
    now = _t.time()
    if symbol in _snapshot_cache:
        ts, data = _snapshot_cache[symbol]
        if now - ts < _SNAPSHOT_TTL:
            return data
    snap = await asyncio.to_thread(get_snapshot, symbol)
    _snapshot_cache[symbol] = (now, snap)
    # 캐시 청소 (50개 초과 시 절반)
    if len(_snapshot_cache) > 50:
        oldest = sorted(_snapshot_cache.items(), key=lambda x: x[1][0])[:25]
        for k, _ in oldest:
            _snapshot_cache.pop(k, None)
    return snap

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
    task1 = asyncio.create_task(alerts_mod.worker(broadcast))
    task2 = asyncio.create_task(morning_brief.daily_scheduler())
    task3 = asyncio.create_task(dart_watcher.worker(broadcast))
    log.info("Toss server ready (alerts + morning brief + DART watcher)")
    try:
        yield
    finally:
        task1.cancel()
        task2.cancel()
        task3.cancel()


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


# ─── Analyze 보조: 매매 실행 증권사 추천 (한국 거주자 기준) ──────
_BROKERS_KR = [
    {"name": "토스증권", "fee": "온라인 0.015%", "url": "https://tossinvest.com",
     "note": "수수료 최저, MTS 직관적", "tag": "수수료 최저"},
    {"name": "키움증권", "fee": "0.015%", "url": "https://www.kiwoom.com",
     "note": "거래량 1위, 신용·대주 풍부", "tag": "유동성 최고"},
    {"name": "미래에셋증권", "fee": "0.014%", "url": "https://securities.miraeasset.com",
     "note": "리서치 리포트 우수", "tag": "리서치"},
    {"name": "삼성증권", "fee": "0.014%", "url": "https://www.samsungpop.com",
     "note": "대형 우량주 중심 매매에 적합", "tag": "대형주"},
]
_BROKERS_US = [
    {"name": "토스증권 (해외주식)", "fee": "0.07%", "url": "https://tossinvest.com",
     "note": "한국에서 미국주식 거래 가장 간편, 환전 자동", "tag": "초보 추천"},
    {"name": "미래에셋증권 (해외주식)", "fee": "0.25% (이벤트 시 0.07%)", "url": "https://securities.miraeasset.com",
     "note": "정규장+프리/애프터마켓 모두 지원", "tag": "프리마켓"},
    {"name": "키움증권 (영웅문Global)", "fee": "0.07~0.25%", "url": "https://www.kiwoom.com",
     "note": "전문가 차트, 옵션/ETF 다양", "tag": "전문가"},
    {"name": "Interactive Brokers (IBKR)", "fee": "$0.005/주 (최소 $1)", "url": "https://www.interactivebrokers.com",
     "note": "글로벌 최저 수수료, 단 영문 가입 필요", "tag": "초저비용"},
]


def _attach_brokers(ana: dict, symbol: str) -> dict:
    is_kr = symbol.isdigit() and len(symbol) == 6
    ana["brokers"] = _BROKERS_KR if is_kr else _BROKERS_US
    return ana


def _attach_intelligence(ana: dict, snap: dict, news: list[dict], flow: dict) -> dict:
    """경쟁 AI 플랫폼 핵심 5기능 일괄 주입.

    1. TOSS Score (단일 종합 점수 0-100)
    2. Move Explainer (왜 움직였나 1문장)
    3. Multi-TF Consensus (1H/1D/1W 시그널 일치도)
    4. Chart Patterns (자동 인식)
    5. Earnings/Analyst (flow에서 추출)
    """
    try:
        ana["toss_score"] = compute_toss_score(snap, ana)
    except Exception as e:
        log.warning(f"toss_score 실패: {e}")

    try:
        ana["move_explainer"] = explain_move(snap, news, ana)
    except Exception as e:
        log.warning(f"move_explainer 실패: {e}")

    try:
        ana["multi_tf"] = compute_multi_tf(snap)
    except Exception as e:
        log.warning(f"multi_tf 실패: {e}")

    try:
        ana["patterns"] = detect_patterns(snap)
    except Exception as e:
        log.warning(f"patterns 실패: {e}")

    try:
        ana["volatility"] = grade_volatility(snap)
    except Exception as e:
        log.warning(f"volatility 실패: {e}")

    # flow에서 어닝/애널리스트를 ana로 끌어올림 (프론트가 한 곳에서 읽도록)
    if flow:
        if flow.get("analyst_consensus"):
            ana["analyst_consensus"] = flow["analyst_consensus"]
        if flow.get("price_target"):
            ana["price_target"] = flow["price_target"]
        if flow.get("earnings_next"):
            ana["earnings_next"] = flow["earnings_next"]

    return ana


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

    # ── 한국주식: KIS 매수/매도 상세 데이터 그대로 노출 (프론트가 표로 그림)
    if is_kr:
        flow_kr = snap.get("flow_kr") or {}
        if any(k in flow_kr for k in ("foreign_buy", "foreign_sell")):
            # 매수/매도 분리 데이터 있음
            ana["flow_table"] = {
                "foreign": {
                    "buy": int(flow_kr.get("foreign_buy") or 0),
                    "sell": int(flow_kr.get("foreign_sell") or 0),
                    "net": int(flow_kr.get("foreign_net_qty") or 0),
                },
                "institutional": {
                    "buy": int(flow_kr.get("institutional_buy") or 0),
                    "sell": int(flow_kr.get("institutional_sell") or 0),
                    "net": int(flow_kr.get("institutional_net_qty") or 0),
                },
                "retail": {
                    "buy": int(flow_kr.get("retail_buy") or 0),
                    "sell": int(flow_kr.get("retail_sell") or 0),
                    "net": int(flow_kr.get("retail_net_qty") or 0),
                },
                "total_buy": int(flow_kr.get("total_buy") or 0),
                "total_sell": int(flow_kr.get("total_sell") or 0),
                "date": flow_kr.get("date"),
            }
            # ── 스마트머니(외국인+기관) vs 개인 비교
            #    주식시장은 매수=매도(zero-sum)이므로 단순 총합 비교는 무의미.
            #    의미있는 시그널: 누가 사고 누가 파는가 → 외국인+기관이 사면 매수 우위.
            f = ana["flow_table"]["foreign"]
            i = ana["flow_table"]["institutional"]
            r = ana["flow_table"]["retail"]
            smart_buy = f["buy"] + i["buy"]
            smart_sell = f["sell"] + i["sell"]
            smart_net = f["net"] + i["net"]    # 외국인+기관 합산 순매수
            retail_net = r["net"]

            ana["flow_table"]["smart_buy"] = smart_buy
            ana["flow_table"]["smart_sell"] = smart_sell
            ana["flow_table"]["smart_net"] = smart_net
            ana["flow_table"]["retail_only_net"] = retail_net

            # 우위 판정: 1) 외국인+기관 순매수 방향 + 2) 의미있는 절대 규모
            #   - 일거래량 대비 5% 이상이어야 시그널로 인정 (노이즈 제거)
            total_vol = ana["flow_table"]["total_buy"]   # = total_sell (zero-sum)
            min_signal = max(total_vol * 0.05, 50000)    # 최소 5% 또는 5만주

            denom = max(abs(smart_net) + abs(retail_net), 1)
            smart_strength = abs(smart_net) / denom * 100

            if abs(smart_net) < min_signal:
                ana["flow_table"]["dominance"] = "방향성 약함"
                ana["flow_table"]["dominance_emoji"] = "⚪"
                ana["flow_table"]["dominance_detail"] = (
                    f"외국인·기관 순매수 {smart_net:+,}주 (거래량 대비 미미) "
                    "— 추세 형성 전 관망 단계"
                )
            elif smart_net > 0:
                ana["flow_table"]["dominance"] = "스마트머니 매수 우위"
                ana["flow_table"]["dominance_emoji"] = "🟢"
                ana["flow_table"]["dominance_detail"] = (
                    f"외국인·기관이 +{smart_net:,}주 순매수, 개인 {retail_net:+,}주 "
                    "→ 상승 시그널 (개인 매도 vs 기관 매집 패턴)"
                    if retail_net < 0 else
                    f"외국인·기관 +{smart_net:,}주, 개인 {retail_net:+,}주 동반 매수 → 강한 매수세"
                )
            else:
                ana["flow_table"]["dominance"] = "스마트머니 매도 우위"
                ana["flow_table"]["dominance_emoji"] = "🔴"
                ana["flow_table"]["dominance_detail"] = (
                    f"외국인·기관이 {smart_net:,}주 순매도, 개인 {retail_net:+,}주 추격매수 "
                    "→ 하락 시그널 (분배 단계 의심)"
                    if retail_net > 0 else
                    f"외국인·기관 {smart_net:,}주, 개인 {retail_net:+,}주 동반 매도 → 강한 매도세"
                )
            ana["flow_table"]["smart_strength_pct"] = round(smart_strength, 1)

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
        snap = await _cached_snapshot(symbol)
        # 캐시에도 시장수급/지능/증권사 보강 (캐시 시점엔 없었던 필드 보충)
        cached = _ensure_flow_fields(cached, snap, symbol)
        # 어닝·애널리스트는 캐시 path에 flow가 없으니 빈 dict로 호출 → 패턴/스코어만 보강
        cached = _attach_intelligence(cached, snap, [], {})
        cached = _attach_brokers(cached, symbol)
        await broadcast({"type": "analysis", "symbol": symbol, "user_id": user["id"]})
        return {"snapshot": snap, "analysis": cached,
                "market_status": market_status_for(symbol),
                "cached": True}

    try:
        log.info(f"Fetching data in parallel for {symbol}")
        bench_sym, bench_name = get_benchmark_symbol(symbol)
        is_kr = symbol.isdigit() and len(symbol) == 6

        snap_task = _cached_snapshot(symbol)
        news_task = fetch_news(symbol)
        profile_task = fetch_profile(symbol)
        flow_task = fetch_market_flow(symbol)
        bench_task = _cached_snapshot(bench_sym)  # 섹터 RS 비교용 벤치마크

        # KR 종목이면 DART 재무 + 인사이더도 병렬 fetch
        tasks = [snap_task, news_task, profile_task, flow_task, bench_task]
        if is_kr:
            from app.dart import get_financials, get_insider_trades
            tasks.extend([get_financials(symbol), get_insider_trades(symbol)])

        results = await asyncio.gather(*tasks, return_exceptions=True)
        snap, news, profile, flow, bench_snap = results[:5]
        dart_financials = results[5] if is_kr and len(results) > 5 and not isinstance(results[5], Exception) else None
        dart_insider = results[6] if is_kr and len(results) > 6 and not isinstance(results[6], Exception) else None
        # 벤치마크는 실패해도 분석은 진행
        if isinstance(bench_snap, Exception):
            bench_snap = None

        watch = next((w for w in await db.list_watch(user["id"]) if w["symbol"] == symbol), None)
        risk_val = watch["risk_pct"] if watch else 1.0

        # KR 종목: DART 재무를 분석에 컨텍스트로 추가
        if is_kr and dart_financials and isinstance(dart_financials, dict) and dart_financials:
            # flow에 dart_financials를 합쳐서 AI 프롬프트에도 들어가도록
            if isinstance(flow, dict):
                flow["dart_financials"] = dart_financials

        ana = await asyncio.to_thread(analyze, symbol, snap, news, flow, profile, risk_val)
        # AI 응답에 시장수급/증권사/지능 필드가 빠져있으면 deterministic 계산해서 주입
        ana = _ensure_flow_fields(ana, snap, symbol)
        ana = _attach_intelligence(ana, snap, news, flow)
        ana = _attach_brokers(ana, symbol)

        # DART 데이터를 ana에 직접 주입 (프론트가 한 곳에서 읽도록)
        if is_kr:
            if dart_financials and isinstance(dart_financials, dict) and dart_financials:
                ana["dart_financials"] = dart_financials
            if dart_insider and isinstance(dart_insider, list) and dart_insider:
                ana["dart_insider"] = dart_insider

        # 섹터 상대강도 — 벤치마크 받은 경우만
        if bench_snap and isinstance(bench_snap, dict):
            try:
                ana["relative_strength"] = compute_relative_strength(snap, bench_snap, bench_name)
            except Exception as e:
                log.warning(f"RS 계산 실패: {e}")

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
                "profile": profile, "news": news[:5], "sizing": sizing,
                "market_status": market_status_for(symbol),
                "cached": False}

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


# ─── 자동 주문 (KIS Open API) ───────────────────────────────────
class OrderIn(BaseModel):
    symbol: str
    side: str  # buy / sell
    qty: int = Field(gt=0, le=10000)  # 1~10000주 제한
    price: float = Field(default=0, ge=0)  # 0=시장가


@app.get("/api/trade/mode")
async def api_trade_mode(_: dict = Depends(get_current_user)):
    """현재 자동주문 모드 확인 — 프론트가 라이브/페이퍼 표시용."""
    live = kis_is_live()
    return {
        "mode": "LIVE" if live else "PAPER",
        "warning": "⚠️ 실전 주문 모드 — 실제 자금이 사용됩니다" if live
                  else "모의투자 모드 — 실제 돈 사용 안 함",
        "max_krw": int(float(os.environ.get("MAX_ORDER_AMOUNT_KRW", "300000"))),
        "max_usd": float(os.environ.get("MAX_ORDER_AMOUNT_USD", "200")),
        "account_set": bool(os.environ.get("KIS_ACCOUNT_NO", "").strip()),
    }


# ─── Telegram 푸시 알림 ────────────────────────────────────────
class TelegramChatIn(BaseModel):
    chat_id: str


@app.get("/api/telegram/info")
async def api_telegram_info(user: dict = Depends(get_current_user)):
    """텔레그램 봇 설정 상태 + 본인 chat_id."""
    bot = await telegram_alert.get_me()
    my_chat_id = await db.get_telegram_chat_id(user["id"])
    return {
        "configured": telegram_alert.is_configured(),
        "bot_username": bot.get("username"),
        "bot_name": bot.get("first_name"),
        "my_chat_id": my_chat_id,
        "subscribed": bool(my_chat_id),
    }


@app.get("/api/telegram/diagnose")
async def api_telegram_diagnose(user: dict = Depends(get_current_user)):
    """텔레그램 풀 진단 — 단계별 상태 + 실제 발송 테스트.

    1) TOKEN 설정 확인
    2) 봇 정보 fetch (TOKEN 유효성)
    3) 본인 chat_id 저장 여부
    4) 실제 테스트 메시지 발송 (가장 확실한 검증)
    5) 봇과 최근 대화한 chat_id 후보
    6) 다음 단계 권장
    """
    diag = {
        "steps": [],
        "next_action": None,
        "bot_info": None,
        "candidates": [],
    }

    # Step 1: TOKEN
    has_token = telegram_alert.is_configured()
    diag["steps"].append({
        "name": "TELEGRAM_BOT_TOKEN 환경변수",
        "status": "ok" if has_token else "fail",
        "detail": "설정됨" if has_token else "Render Environment에 미설정",
    })
    if not has_token:
        diag["next_action"] = "Render → Environment → TELEGRAM_BOT_TOKEN 추가 후 자동 재배포 대기 (5분)"
        return diag

    # Step 2: Bot info (TOKEN 유효성)
    bot = await telegram_alert.get_me()
    if bot and bot.get("username"):
        diag["bot_info"] = {
            "username": bot.get("username"),
            "name": bot.get("first_name"),
            "url": f"https://t.me/{bot['username']}",
        }
        diag["steps"].append({
            "name": "봇 정보 조회",
            "status": "ok",
            "detail": f"@{bot['username']} ({bot.get('first_name', '')})",
        })
    else:
        diag["steps"].append({
            "name": "봇 정보 조회",
            "status": "fail",
            "detail": "TOKEN이 잘못됨 — BotFather에서 토큰 재확인 필요",
        })
        diag["next_action"] = "BotFather → /mybots → 본인 봇 → API Token 확인 후 Render 환경변수 갱신"
        return diag

    # Step 3: 본인 chat_id
    chat_id = await db.get_telegram_chat_id(user["id"])
    diag["steps"].append({
        "name": "내 chat_id 등록",
        "status": "ok" if chat_id else "warn",
        "detail": f"chat_id: {chat_id}" if chat_id else "아직 등록 안 됨",
    })

    # Step 5: 봇과 최근 대화한 후보 조회 (chat_id 등록 안 됐을 때 도움)
    candidates = await telegram_alert.discover_chat_ids()
    diag["candidates"] = candidates

    # Step 4: 실제 발송 테스트 (chat_id 있을 때)
    if chat_id:
        send_res = await telegram_alert.send(
            chat_id,
            "🔍 <b>Toss 진단 메시지</b>\n\n이 메시지가 보이면 텔레그램 알림이 정상 작동 중입니다.",
        )
        if send_res.get("ok"):
            diag["steps"].append({
                "name": "실제 메시지 발송 테스트",
                "status": "ok",
                "detail": "✅ 텔레그램에 진단 메시지 도착했어야 함 (앱 확인)",
            })
            diag["next_action"] = "✅ 모든 단계 정상. 텔레그램 앱 열어서 진단 메시지 확인하세요."
        else:
            err = send_res.get("error", "")
            # 가장 흔한 케이스: chat_id가 옛 봇 거 → 새 봇은 모름
            invalid_chat = "chat not found" in err.lower() or "bot was blocked" in err.lower()
            diag["steps"].append({
                "name": "실제 메시지 발송 테스트",
                "status": "fail",
                "detail": f"❌ 발송 실패: {err}",
            })
            if invalid_chat:
                # 옛 chat_id 자동 정리
                await db.set_telegram_chat_id(user["id"], "")
                diag["next_action"] = (
                    "❌ 옛 chat_id가 새 봇에 매칭 안 됨. 자동으로 chat_id 초기화함. "
                    f"이제 텔레그램에서 @{bot['username']} 검색 → /start 입력 → 다시 [등록] 클릭"
                )
            else:
                diag["next_action"] = f"❌ 발송 실패 — {err}. BotFather에서 봇 상태 확인 권장."
    else:
        # chat_id 없음 — 후보 있으면 안내
        if candidates:
            diag["next_action"] = (
                f"📲 봇과 대화 OK ({len(candidates)}명 발견). "
                "프로필 → 텔레그램 알림 → 본인 chat_id 클릭하여 [등록]"
            )
        else:
            diag["next_action"] = (
                f"📱 텔레그램에서 @{bot['username']} 검색 → /start 입력 → "
                "다시 이 페이지로 돌아와 [등록] 클릭"
            )

    return diag


@app.get("/api/telegram/discover")
async def api_telegram_discover(_: dict = Depends(get_current_user)):
    """봇과 최근 대화한 chat_id 후보 — 본인 ID 찾기 도우미."""
    if not telegram_alert.is_configured():
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다")
    return {"candidates": await telegram_alert.discover_chat_ids()}


@app.post("/api/telegram/subscribe")
async def api_telegram_subscribe(body: TelegramChatIn, user: dict = Depends(get_current_user)):
    """본인 chat_id 등록 + 테스트 메시지 즉시 발송."""
    if not telegram_alert.is_configured():
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN 미설정 — 관리자 설정 필요")
    chat_id = body.chat_id.strip()
    if not chat_id:
        raise HTTPException(400, "chat_id 비어있음")

    test_msg = (
        "✅ <b>Toss 알림 연결 완료</b>\n\n"
        f"안녕하세요 <b>{user.get('display_name') or user.get('username')}</b>님,\n"
        "이제 매수/매도/익절/손절 시그널이 발생할 때마다\n"
        "이 채팅으로 즉시 푸시됩니다.\n\n"
        "📈 워치리스트와 포트폴리오를 분석하면 자동으로\n"
        "    실시간 알림이 시작됩니다."
    )
    res = await telegram_alert.send(chat_id, test_msg)
    if not res["ok"]:
        raise HTTPException(400, f"테스트 발송 실패: {res['error']}")

    await db.set_telegram_chat_id(user["id"], chat_id)
    return {"ok": True, "chat_id": chat_id}


@app.delete("/api/telegram/subscribe")
async def api_telegram_unsubscribe(user: dict = Depends(get_current_user)):
    await db.set_telegram_chat_id(user["id"], "")
    return {"ok": True}


# ─── 모의 트레이드 트래커 (시그널 성과 검증) ────────────────────
@app.get("/api/mock-trades")
async def api_mock_trades(user: dict = Depends(get_current_user)):
    return await db.list_user_mock_trades(user["id"])


@app.get("/api/mock-trades/stats")
async def api_mock_stats(user: dict = Depends(get_current_user)):
    return await db.mock_trade_stats(user["id"])


# ─── 백테스트 (과거 데이터로 시그널 검증) ────────────────────────
@app.get("/api/backtest/{symbol}")
async def api_backtest(symbol: str, hold_days: int = 3,
                       _: dict = Depends(get_current_user)):
    if hold_days < 1 or hold_days > 30:
        raise HTTPException(400, "hold_days는 1~30")
    return await asyncio.to_thread(run_backtest, symbol.upper(), hold_days)


# ─── 스마트 스캐너 — 인기 40종목 자동 분석 → TOP 후보 ────────────
@app.get("/api/scan/today")
async def api_scan_today(market: str = "BOTH", limit: int = 5,
                         ranking: str = "volume",
                         force: bool = False,
                         _: dict = Depends(get_current_user)):
    """KR/US/BOTH 인기 종목 스캔 → TOSS Score 상위 N개. 5분 캐시.

    KR ranking 옵션:
    - volume: 거래량 TOP (기본)
    - value: 시총 TOP (대형주 중심)
    - rise: 상승률 TOP (모멘텀)
    - fall: 하락률 TOP (역발상)
    - foreign: 외국인 순매수 TOP (스마트머니)
    - popular: 정적 인기 종목
    """
    if market not in ("KR", "US", "BOTH"):
        raise HTTPException(400, "market은 KR/US/BOTH")
    if ranking not in ("volume", "value", "rise", "fall", "foreign", "popular"):
        raise HTTPException(400, "ranking은 volume/value/rise/fall/foreign/popular")
    picks = await get_top_picks(force=force, market=market, limit=limit,
                                kr_ranking=ranking)
    return {"picks": picks, "count": len(picks), "ranking": ranking,
            "updated_at": time.time()}


# ─── 시장 지수 (Bloomberg-style ticker) ──────────────────────────
@app.get("/api/indices")
async def api_indices(_: dict = Depends(get_current_user)):
    """KOSPI/KOSDAQ/S&P500/NASDAQ/Dow 실시간. 30초 캐시."""
    return {"indices": await fetch_indices()}


# ─── 사용자 지정 가격 알림 ───────────────────────────────────────
class PriceAlertIn(BaseModel):
    symbol: str
    target_price: float = Field(gt=0)
    condition: str = ">="  # >= / <= / ==
    note: str = ""


@app.get("/api/price-alerts")
async def api_list_price_alerts(user: dict = Depends(get_current_user)):
    return await db.list_user_price_alerts(user["id"], only_active=False)


@app.post("/api/price-alerts")
async def api_add_price_alert(body: PriceAlertIn, user: dict = Depends(get_current_user)):
    if body.condition not in (">=", "<=", "=="):
        raise HTTPException(400, "condition은 >= / <= / ==")
    aid = await db.add_price_alert(
        user["id"], body.symbol.upper(), body.target_price,
        body.condition, body.note
    )
    return {"ok": True, "id": aid}


@app.delete("/api/price-alerts/{alert_id}")
async def api_delete_price_alert(alert_id: int, user: dict = Depends(get_current_user)):
    await db.delete_price_alert(alert_id, user["id"])
    return {"ok": True}


# ─── 포트폴리오 리스크 분석 ──────────────────────────────────────
@app.get("/api/portfolio/risk")
async def api_portfolio_risk(user: dict = Depends(get_current_user)):
    """집중도 / 분산도 / 시장 비중 + 경고 자동 생성."""
    holdings = await db.list_portfolio(user["id"])
    if not holdings:
        return {"ok": False, "msg": "보유 종목 없음"}

    # 현재가 fetch (병렬, 캐시 활용)
    ticks: dict[str, float] = {}
    async def _t(sym):
        try:
            snap = await _cached_snapshot(sym)
            ticks[sym] = float(snap.get("quote", {}).get("price") or 0)
        except Exception:
            ticks[sym] = 0
    await asyncio.gather(*[_t(h["symbol"]) for h in holdings])

    return analyze_portfolio_risk(holdings, ticks)


# ─── DART 공시 최근 N일 조회 ─────────────────────────────────────
@app.get("/api/dart/{symbol}")
async def api_dart_filings(symbol: str, days: int = 7,
                            _: dict = Depends(get_current_user)):
    """KR 종목의 최근 공시 (DART OpenAPI)."""
    sym = symbol.upper()
    if not (sym.isdigit() and len(sym) == 6):
        raise HTTPException(400, "한국 종목 코드 (6자리 숫자)만 지원")
    filings = await dart_watcher._fetch_dart_filings(sym, days=days)
    out = []
    for f in filings:
        icon, category = dart_watcher._classify_filing(f.get("report_nm", ""))
        out.append({
            "rcept_no": f.get("rcept_no"),
            "rcept_dt": f.get("rcept_dt"),
            "report_nm": f.get("report_nm"),
            "submitter": f.get("flr_nm"),
            "icon": icon,
            "category": category,
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={f.get('rcept_no', '')}",
        })
    return {"symbol": sym, "filings": out, "count": len(out)}


# ─── 모닝 브리프 ─────────────────────────────────────────────────
@app.get("/api/morning-brief/preview")
async def api_morning_preview(_: dict = Depends(get_current_user)):
    """모닝 브리프 미리보기 (HTML 텍스트)."""
    text = await morning_brief.generate_brief()
    return {"text": text}


@app.post("/api/morning-brief/send-now")
async def api_morning_send(_: dict = Depends(get_current_user)):
    """수동으로 지금 모닝 브리프 발송 (테스트용)."""
    sent = await morning_brief.send_to_all()
    return {"ok": True, "sent_to": sent}


# ─── 헬스 체크 — 외부 API 상태 점검 ──────────────────────────────
@app.get("/api/health")
async def api_health():
    """주요 데이터 소스 상태."""
    out = {"status": "ok"}
    out["gemini"] = "configured" if os.environ.get("GEMINI_API_KEY") else "missing"
    out["finnhub"] = "configured" if os.environ.get("FINNHUB_API_KEY") else "missing"
    out["alpaca"] = "configured" if os.environ.get("ALPACA_API_KEY") else "missing"
    out["kis"] = "configured" if os.environ.get("KIS_APP_KEY") else "missing"
    out["kis_account"] = "set" if os.environ.get("KIS_ACCOUNT_NO") else "missing"
    out["kis_live"] = "ENABLED" if kis_is_live() else "PAPER"
    out["telegram"] = "configured" if telegram_alert.is_configured() else "missing"
    out["naver"] = "configured" if os.environ.get("NAVER_CLIENT_ID") else "missing"
    out["dart"] = "configured" if os.environ.get("DART_API_KEY") else "missing"
    out["jwt_secret"] = "set" if os.environ.get("JWT_SECRET_KEY") else "ephemeral"
    return out


# ─── 텔레그램 테스트 발송 (등록된 chat_id로 샘플 알림) ───────────
@app.post("/api/telegram/test")
async def api_telegram_test(user: dict = Depends(get_current_user)):
    chat_id = await db.get_telegram_chat_id(user["id"])
    if not chat_id:
        raise HTTPException(400, "텔레그램 chat_id 미등록 — 먼저 /api/telegram/subscribe")
    if not telegram_alert.is_configured():
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN 미설정")

    sample = telegram_alert.format_alert(
        symbol="005930", kind="BUY", price=72500,
        message="✅ 봇 연결 테스트 — 실제 매매 신호 아님. 이렇게 알림이 도착합니다.",
        toss_score={"score": 78.5, "grade": "A"},
        entry=72500, target=75000, stop=71000, is_kr=True,
    )
    res = await telegram_alert.send(chat_id, sample)
    if not res["ok"]:
        raise HTTPException(400, f"발송 실패: {res['error']}")
    return {"ok": True, "msg": "텔레그램에서 메시지 확인하세요"}


@app.post("/api/trade/order")
async def api_trade_order(body: OrderIn, user: dict = Depends(get_current_user)):
    """KIS API로 실제 주문 — 한+미 자동 분기.

    안전: 1회 주문 금액 한도 + LIVE 명시 옵트인 + 사용자 클릭 필수.
    """
    side = body.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side는 buy 또는 sell")

    res = await asyncio.to_thread(auto_order, body.symbol, side, body.qty, body.price)

    if res.get("ok"):
        # 매매 기록 자동 저장 (audit log)
        try:
            await db.add_trade(
                user["id"], body.symbol,
                "BUY" if side == "buy" else "SELL",
                body.qty, body.price or 0,
                f"자동주문 [{res.get('mode')}] 주문번호 {res.get('order_no')}"
            )
        except Exception as e:
            log.warning(f"trade audit log 실패: {e}")
        log.info(f"order ok: user={user['id']} {side} {body.symbol} x{body.qty} @ {body.price} [{res['mode']}]")
    else:
        log.warning(f"order fail: user={user['id']} {body.symbol} - {res.get('error')}")

    return res


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
            snap = await _cached_snapshot(symbol)
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
    sym = data["symbol"].upper()
    entry_price = float(data["entry_price"])
    krw_invested = float(data["krw_invested"])
    shares = float(data.get("shares", 0) or 0)

    await db.add_to_portfolio(user["id"], sym, entry_price, krw_invested, shares)

    # 백그라운드: 분석 + 텔레그램 알림 (사용자 응답은 즉시)
    asyncio.create_task(_post_portfolio_add(
        sym, user["id"], entry_price, shares, krw_invested
    ))

    return {"ok": True}


async def _post_portfolio_add(symbol: str, user_id: int,
                              entry_price: float, shares: float, krw_invested: float):
    """포트폴리오 추가 후처리 — 분석(plan 생성) + 텔레그램 즉시 알림 + 토스 정보."""
    try:
        # 1) 종목명 + 현재가 + (가능하면) 분석 plan
        existing_plan = await db.get_plan(symbol, max_age_sec=3600)
        ana = existing_plan
        snap = None
        profile = None

        try:
            # 시세는 항상 새로 (현재가 정확히)
            snap = await _cached_snapshot(symbol)
            profile_task = fetch_profile(symbol)
            news_task = fetch_news(symbol) if not ana else asyncio.sleep(0)
            flow_task = fetch_market_flow(symbol) if not ana else asyncio.sleep(0)
            profile, news_or_none, flow_or_none = await asyncio.gather(
                profile_task, news_task, flow_task, return_exceptions=True
            )
            if isinstance(profile, Exception):
                profile = {"name": symbol}

            # plan 없으면 분석 실행 → 알림 트리거 활성화
            if not ana and not isinstance(news_or_none, Exception) and not isinstance(flow_or_none, Exception):
                ana = await asyncio.to_thread(
                    analyze, symbol, snap, news_or_none, flow_or_none, profile, 1.0
                )
                ana = _ensure_flow_fields(ana, snap, symbol)
                ana = _attach_intelligence(ana, snap, news_or_none, flow_or_none)
                await db.save_plan(symbol, ana)
                log.info(f"포트폴리오 추가 → 백그라운드 분석 완료: {symbol}")
        except Exception as e:
            log.warning(f"포트폴리오 분석 실패 {symbol}: {e}")

        # 2) 텔레그램 알림
        chat_id = await db.get_telegram_chat_id(user_id)
        if not chat_id or not telegram_alert.is_configured():
            return

        current_price = (snap or {}).get("quote", {}).get("price") if snap else None
        name = (profile or {}).get("name") or symbol
        text = telegram_alert.format_portfolio_added(
            symbol=symbol,
            name=name,
            entry_price=entry_price,
            shares=shares,
            krw_invested=krw_invested,
            current_price=current_price,
            ana=ana,
        )
        await telegram_alert.send(chat_id, text)
        log.info(f"포트폴리오 알림 발송: user {user_id} → {symbol}")
    except Exception:
        log.exception(f"_post_portfolio_add error {symbol}")

@app.delete("/api/portfolio/{pid}")
async def api_remove_portfolio(pid: int, user: dict = Depends(get_current_user)):
    await db.remove_from_portfolio(pid, user["id"])
    return {"ok": True}


@app.post("/api/portfolio/{pid}/add")
async def api_average_down(pid: int, data: dict, user: dict = Depends(get_current_user)):
    """추매 — 평단가 자동 재계산 (가중 평균)."""
    add_shares = float(data.get("shares") or 0)
    add_price = float(data.get("price") or 0)
    if add_shares <= 0 or add_price <= 0:
        raise HTTPException(400, "shares와 price는 0보다 커야 합니다")

    result = await db.average_down(pid, user["id"], add_shares, add_price)
    if not result:
        raise HTTPException(404, "포지션을 찾을 수 없거나 권한이 없습니다")
    if result.get("error"):
        raise HTTPException(400, result["error"])

    # 텔레그램 알림 (추매 결과)
    item = await db.get_portfolio_item(pid, user["id"])
    if item:
        sym = item["symbol"]
        chat_id = await db.get_telegram_chat_id(user["id"])
        if chat_id and telegram_alert.is_configured():
            asyncio.create_task(_notify_average_down(chat_id, sym, result))

    return result


async def _notify_average_down(chat_id: str, symbol: str, r: dict):
    """추매 텔레그램 알림 — 새 평단가 + 변화율."""
    is_kr = symbol.isdigit() and len(symbol) == 6
    cur = "₩" if is_kr else "$"
    fmt = (lambda v: f"{cur}{int(v):,}") if is_kr else (lambda v: f"{cur}{v:.2f}")

    direction = "🟢 평단가 하락 (물타기 성공)" if r["new_avg"] < r["old_avg"] else "🟠 평단가 상승 (불타기)"

    msg = (
        f"<b>📥 추매 완료 — {symbol}</b>\n"
        f"\n"
        f"{direction}\n"
        f"\n"
        f"기존 보유: <code>{r['old_shares']:g}주 @ {fmt(r['old_avg'])}</code>\n"
        f"이번 추매: <code>+{r['added_shares']:g}주 @ {fmt(r['added_price'])}</code>\n"
        f"\n"
        f"<b>새 평단가: {fmt(r['new_avg'])}</b> "
        f"({r['avg_change_pct']:+.2f}%)\n"
        f"총 보유: <b>{r['new_shares']:g}주</b>\n"
        f"총 투입: <b>{fmt(r['total_invested'])}</b>"
    )
    await telegram_alert.send(chat_id, msg)

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
