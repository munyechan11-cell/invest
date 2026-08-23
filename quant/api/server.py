"""REST + WebSocket control plane and dashboard.

Deliberately not a user system: there are no accounts, plans, or tiers. There
is exactly one optional shared token (`QUANT_API_TOKEN`) because this API can
start and stop a live trading bot, and an unauthenticated endpoint that can do
that should not be reachable from the internet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from quant.config.loader import load_config
from quant.config.schema import StrategyConfig
from quant.core.events import Event, EventType
from quant.core.types import UTC, RunMode
from quant.live.credentials import (
    CredentialStore, OPERATOR_FIELDS, VENUES_BY_ID, load_env_file,
    venue_catalog,
)
from quant.live.profile import ProfileStore, questionnaire, score_answers
from quant.live.state import StateStore

log = logging.getLogger("quant.api")
STATIC_DIR = Path(__file__).parent / "static"


class Hub:
    """Fan-out of engine events to connected WebSocket clients."""

    def __init__(self, ring_size: int = 500):
        self.clients: set[WebSocket] = set()
        self.ring: list[dict] = []
        self.ring_size = ring_size

    async def publish(self, event: Event) -> None:
        payload = {
            "type": event.type.value,
            "ts": event.ts.isoformat(),
            "source": event.source,
            "payload": event.payload,
        }
        self.ring.append(payload)
        if len(self.ring) > self.ring_size:
            del self.ring[: len(self.ring) - self.ring_size]
        if not self.clients:
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)

    def recent(self, limit: int = 100, types: set[str] | None = None) -> list[dict]:
        items = self.ring if types is None else [e for e in self.ring if e["type"] in types]
        return items[-limit:]


class AppState:
    def __init__(self, config: StrategyConfig | None, state_path: str):
        self.config = config
        self.state_path = state_path
        self.hub = Hub()
        self.trader: Any = None
        self.trader_task: asyncio.Task | None = None
        self.backtests: dict[str, dict] = {}
        self.started_at = datetime.now(UTC)


class BacktestRequest(BaseModel):
    config_path: Optional[str] = None
    config: Optional[dict] = None
    start: Optional[str] = None
    end: Optional[str] = None
    starting_cash: Optional[float] = None


class ManualOrderRequest(BaseModel):
    ticker: str
    quantity: Optional[float] = None
    notional: Optional[float] = None
    limit_price: Optional[float] = None
    #: hand the resulting position back to the strategy instead of pinning it
    manage: bool = False
    note: str = ""


class ProfileRequest(BaseModel):
    """진단 답안 {question_id: option_id}"""

    answers: dict[str, str] = Field(default_factory=dict)


class ProfileOverrideRequest(BaseModel):
    """마이페이지에서 축을 직접 조정할 때. -1.0 ~ +1.0"""

    overrides: dict[str, float] = Field(default_factory=dict)


class SetupRequest(BaseModel):
    """Values from the setup form. Blank fields leave existing ones alone."""

    values: dict[str, str] = Field(default_factory=dict)


class LimitsRequest(BaseModel):
    """Daily caps. Omitted fields are left alone; an explicit 0 removes a cap.

    Partial by design: a client that raises the order count must not silently
    release the loss cap it never mentioned.
    """

    max_daily_notional: Optional[float] = None
    max_daily_orders: Optional[int] = None
    max_daily_loss: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None


#: (요청 필드, TradingBudget 속성, .env 키, 변환)
_LIMIT_FIELDS = (
    ("max_daily_notional", "max_notional", "QUANT_LIMIT_DAILY_NOTIONAL", float),
    ("max_daily_orders", "max_orders", "QUANT_LIMIT_DAILY_ORDERS", int),
    ("max_daily_loss", "max_loss", "QUANT_LIMIT_DAILY_LOSS",
     lambda v: abs(float(v))),
    ("max_daily_loss_pct", "max_loss_pct", "QUANT_LIMIT_DAILY_LOSS_PCT",
     lambda v: abs(float(v))),
)


class StartRequest(BaseModel):
    config_path: str
    mode: str = Field(default="dry_run", pattern="^(dry_run|live)$")
    #: 실거래일 때만 필요합니다. `quant live` 가 콘솔에서 받는 것과 같은 확인 —
    #: 전략 이름을 정확히 적어야 합니다.
    confirm: str = ""


#: 이 주소들만 "내 컴퓨터에서만 보인다" 고 말할 수 있습니다.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class UnsafeBind(RuntimeError):
    """공개 인터페이스에 토큰 없이 붙이려 할 때."""


def assert_safe_to_bind(host: str) -> None:
    """토큰 없는 API 를 외부에 노출하려 하면 뜨지 않습니다.

    이전에는 경고 한 줄만 찍고 그대로 떴습니다. 그런데 호스팅 플랫폼은
    예외 없이 0.0.0.0 바인딩을 요구하므로, 하필 실제 배포 구성에서만
    경고가 무시되고 매수·매도·전량청산 엔드포인트가 인증 없이 열립니다.
    로그로 남길 성질이 아니라 뜨지 말아야 할 상태입니다.
    """
    if host.strip().lower() in _LOOPBACK:
        return
    if os.environ.get("QUANT_API_TOKEN", "").strip():
        return
    raise UnsafeBind(
        f"{host} 로 바인딩하려는데 QUANT_API_TOKEN 이 없습니다. "
        "이 API 는 매수·매도·전량청산·자격증명 저장을 수행하므로 "
        "인증 없이 외부에 열 수 없습니다.\n"
        "  해결: QUANT_API_TOKEN=$(python3 -c \"import secrets;print(secrets.token_urlsafe(32))\") "
        "를 환경변수로 설정하거나, --host 127.0.0.1 로 로컬에서만 여세요."
    )


def assert_live_start_allowed(config: StrategyConfig, confirm: str) -> None:
    """`quant live` 가 요구하는 것과 정확히 같은 조건을 API 에도 건다.

    대시보드가 실거래로 가는 더 쉬운 길이 되면 안 됩니다. CLI 는 세 가지를
    요구합니다 — 설정 파일 자체가 mode: live 일 것, live_trading_confirmed
    일 것, 사람이 전략 이름을 직접 입력할 것. API 는 두 번째만 손으로 검사했고
    `cfg.mode = ...` 대입은 pydantic 검증을 다시 돌리지 않으므로, '하루 한도
    없는 실거래'가 POST 한 번으로 만들어졌습니다.
    """
    if config.mode is not RunMode.LIVE:
        raise HTTPException(
            400,
            f"설정 파일 mode 가 {config.mode.value} 입니다. "
            "실거래는 설정 파일이 직접 mode: live 라고 선언한 경우에만 시작합니다 "
            "— API 로 모드를 바꿔 실거래로 넘어갈 수는 없습니다.",
        )
    if not config.broker.live_trading_confirmed:
        raise HTTPException(
            400,
            "refusing to start live trading: set broker.live_trading_confirmed "
            "in the config file, not through the API",
        )
    if confirm.strip() != config.name:
        raise HTTPException(
            400,
            f'실거래 확인이 필요합니다: confirm 에 전략 이름 "{config.name}" 을 '
            "정확히 넣으세요 (CLI 가 콘솔에서 묻는 것과 같은 확인입니다).",
        )


def create_app(config: StrategyConfig | None = None,
               state_path: str = "quant_state.db") -> FastAPI:
    # The setup screen writes credentials to .env; load them before anything
    # reads os.environ, or a fully configured operator still sees "key required".
    load_env_file(os.environ.get("QUANT_ENV_FILE", ".env"))

    state = AppState(config, state_path)
    token = os.environ.get("QUANT_API_TOKEN", "").strip()

    async def require_token(request: Request) -> None:
        if not token:
            return
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else \
            request.query_params.get("token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "invalid or missing API token")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not token:
            log.warning(
                "QUANT_API_TOKEN is not set — the control API is unauthenticated. "
                "Bind to localhost or set a token before exposing this."
            )
        yield
        if state.trader is not None:
            state.trader.running = False
        if state.trader_task is not None:
            state.trader_task.cancel()

    app = FastAPI(title="Quant Engine", version="1.0.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        # No cross-origin caller by default, token or not. The dashboard is
        # served by this same app, so it never needs a CORS header; the only
        # thing "*" bought was letting any page the operator happens to visit
        # POST /api/manual/close_all or /api/setup at their own localhost.
        # Anyone genuinely serving the UI from elsewhere names it explicitly.
        allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o],
        allow_methods=["*"], allow_headers=["*"],
    )
    app.state.quant = state
    app.state.api_token = token

    # ── read-only ────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "version": "1.0.0",
            "uptime_s": round((datetime.now(UTC) - state.started_at).total_seconds(), 1),
            "trader_running": bool(state.trader and state.trader.running),
            "authenticated": bool(token),
        }

    @app.get("/api/config")
    async def get_config(_=Depends(require_token)):
        if state.config is None:
            raise HTTPException(404, "no config loaded")
        data = json.loads(state.config.model_dump_json())
        return _redact(data)

    @app.get("/api/status")
    async def status(_=Depends(require_token)):
        if state.trader is None:
            return {"running": False, "message": "no trader started"}
        return state.trader.status()

    @app.get("/api/equity")
    async def equity(limit: int = 2000, _=Depends(require_token)):
        store = StateStore(state.state_path)
        try:
            if state.config:
                store.resume_run(state.config.name, state.config.mode.value)
            return {"points": store.equity_curve(limit)}
        finally:
            store.close()

    @app.get("/api/trades")
    async def trades(limit: int = 100, _=Depends(require_token)):
        store = StateStore(state.state_path)
        try:
            if state.config:
                store.resume_run(state.config.name, state.config.mode.value)
            return {"trades": store.recent_trades(limit)}
        finally:
            store.close()

    @app.get("/api/events")
    async def events(limit: int = 100, type: Optional[str] = None,
                     _=Depends(require_token)):
        types = {t.strip() for t in type.split(",")} if type else None
        return {"events": state.hub.recent(limit, types)}

    @app.get("/api/desk")
    async def desk(limit: int = 20, _=Depends(require_token)):
        """Desk status plus recent deliberations, newest first.

        This is what the trading-floor view renders: one entry per full
        ten-seat deliberation, including every seat's own output so the debate
        can be replayed rather than just summarised.
        """
        trader = state.trader
        model = trader.desk() if trader is not None else None
        if model is None:
            return {"enabled": False,
                    "message": "no desk configured — add an alpha of type 'desk'",
                    "deliberations": []}
        status = model.status()
        status["deliberations"] = [d.to_dict() for d in reversed(model.history[-limit:])]
        status.pop("latest", None)
        return status

    @app.get("/api/desk/{ticker}")
    async def desk_symbol(ticker: str, _=Depends(require_token)):
        trader = state.trader
        model = trader.desk() if trader is not None else None
        if model is None:
            raise HTTPException(404, "no desk configured")
        for decision in reversed(model.history):
            if decision.ticker.upper() == ticker.upper():
                return decision.to_dict()
        raise HTTPException(404, f"no deliberation recorded for {ticker}")

    @app.get("/api/flow")
    async def flow(symbol: Optional[str] = None, window: int = 20,
                   sessions: int = 30, _=Depends(require_token)):
        """투자자별 수급 — foreign / institution / retail net buying."""
        trader = state.trader
        if trader is None:
            return {"available": False, "message": "no trader running", "symbols": {}}
        feed = getattr(trader.engine, "flow_feed", None)
        if feed is None or not feed.has_data:
            return {"available": False,
                    "message": "flow feed is empty — set flow.provider in the config",
                    "failures": getattr(feed, "failures", {}), "symbols": {}}
        ctx = trader.engine.ctx
        wanted = [s for s in ctx.universe
                  if symbol is None or s.ticker.upper() == symbol.upper()]
        out = {}
        for sym in wanted:
            summary = feed.summary(sym, ctx.now, window)
            recent = feed.get(sym, ctx.now)[-sessions:]
            out[sym.ticker] = {
                "summary": summary.to_dict() if summary else None,
                "sessions": [f.to_dict() for f in recent],
            }
        return {"available": True, "window": window, "symbols": out}

    @app.get("/api/universe")
    async def universe(_=Depends(require_token)):
        """Symbols with their last mark — drives the ticker tape."""
        trader = state.trader
        if trader is None:
            return {"symbols": []}
        ctx = trader.engine.ctx
        out = []
        for sym in ctx.universe:
            bars = ctx.history(sym, 2)
            last = bars[-1] if bars else None
            prev = bars[0] if len(bars) > 1 else None
            change = ((last.close / prev.close - 1) * 100
                      if last and prev and prev.close > 0 else 0.0)
            out.append({
                "ticker": sym.ticker, "venue": sym.venue,
                "currency": sym.quote_currency,
                "price": round(last.close, 4) if last else None,
                "change_pct": round(change, 2),
                "invested": ctx.is_invested(sym),
            })
        return {"symbols": out}

    # ── 수동 개입 ────────────────────────────────────────────────────────
    def _require_trader():
        if state.trader is None:
            raise HTTPException(404, "실행 중인 트레이더가 없습니다")
        return state.trader

    def _resolve(trader, ticker: str):
        ctx = trader.engine.ctx
        wanted = ticker.strip().upper()
        for sym in ctx.universe:
            if sym.ticker.upper() == wanted:
                return sym
        for pos in ctx.portfolio.open_positions:
            if pos.symbol.ticker.upper() == wanted:
                return pos.symbol
        raise HTTPException(404, f"유니버스에도 보유에도 없는 종목: {ticker}")

    @app.get("/api/manual")
    async def manual_status(_=Depends(require_token)):
        trader = state.trader
        if trader is None:
            return {"running": False, "paused": False, "pending": [], "recent": []}
        engine = trader.engine
        return {
            "running": True,
            **engine.manual.status(),
            "pinned": engine.ctx.pinned,
            "budget": engine.budget.status() if engine.budget.configured else None,
        }

    @app.post("/api/manual/buy")
    async def manual_buy(req: ManualOrderRequest, _=Depends(require_token)):
        trader = _require_trader()
        symbol = _resolve(trader, req.ticker)
        if req.quantity is None and req.notional is None:
            raise HTTPException(400, "수량 또는 금액 중 하나는 지정해야 합니다")
        from decimal import Decimal

        request = trader.engine.manual.buy(
            symbol,
            quantity=Decimal(str(req.quantity)) if req.quantity is not None else None,
            notional=req.notional, limit_price=req.limit_price,
            manage=req.manage, note=req.note or "대시보드 수동 매수",
        )
        return {"queued": request.to_dict(),
                "note": "다음 봉 처리 시 브로커 안전장치(주문 한도·일일 한도)를 거쳐 발주됩니다"}

    @app.post("/api/manual/sell")
    async def manual_sell(req: ManualOrderRequest, _=Depends(require_token)):
        trader = _require_trader()
        symbol = _resolve(trader, req.ticker)
        from decimal import Decimal

        request = trader.engine.manual.sell(
            symbol,
            quantity=Decimal(str(req.quantity)) if req.quantity is not None else None,
            notional=req.notional, limit_price=req.limit_price,
            note=req.note or "대시보드 수동 매도",
        )
        return {"queued": request.to_dict()}

    @app.post("/api/manual/close")
    async def manual_close(req: ManualOrderRequest, _=Depends(require_token)):
        trader = _require_trader()
        symbol = _resolve(trader, req.ticker)
        return {"queued": trader.engine.manual.close(
            symbol, note=req.note or "대시보드 수동 청산").to_dict()}

    @app.post("/api/manual/close_all")
    async def manual_close_all(_=Depends(require_token)):
        trader = _require_trader()
        return {"queued": trader.engine.manual.close_all(
            note="대시보드 전체 청산").to_dict()}

    @app.post("/api/manual/pause")
    async def manual_pause(_=Depends(require_token)):
        trader = _require_trader()
        trader.engine.manual.pause("대시보드에서 일시정지")
        return {"paused": True,
                "note": "신규 진입만 중단됩니다. 손절·청산·수동주문은 계속 동작합니다."}

    @app.post("/api/manual/resume")
    async def manual_resume(_=Depends(require_token)):
        trader = _require_trader()
        trader.engine.manual.resume()
        return {"paused": False}

    @app.post("/api/manual/unpin/{ticker}")
    async def manual_unpin(ticker: str, _=Depends(require_token)):
        trader = _require_trader()
        trader.engine.ctx.unpin(_resolve(trader, ticker))
        return {"unpinned": ticker, "note": "이 종목을 다시 전략이 관리합니다"}

    @app.post("/api/limits")
    async def limits_save(req: LimitsRequest, _=Depends(require_token)):
        """Apply daily caps now, and persist them for the next restart.

        **Partial.** Only the caps named in the request body change; a field
        that is absent (or null) is left exactly as it was, in the running bot
        and in `.env` alike. Sending an explicit `0` removes that cap — which
        means unlimited — and the response names every cap it removed.

        Written to `.env` rather than back into the strategy YAML, so the
        operator's file — comments and all — is never reformatted by a UI.
        """
        store = CredentialStore(os.environ.get("QUANT_ENV_FILE", ".env"))
        budget = state.trader.engine.budget if state.trader is not None else None
        write: dict[str, str] = {}
        clear: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        for field_name, attr, env_key, cast in _LIMIT_FIELDS:
            sent = getattr(req, field_name)
            if sent is None:
                continue
            value = cast(sent)
            previous = (getattr(budget, attr) if budget is not None
                        else float(os.environ.get(env_key, 0) or 0))
            updated.append(field_name)
            if value:
                write[env_key] = str(value)
            else:
                clear.append(env_key)
                if previous:
                    removed.append(field_name)
            if budget is not None:
                setattr(budget, attr, value)
        if write:
            store.update(write)
        if clear:
            store.remove(clear)
        applied = budget.status() if budget is not None else None
        note = ("실행 중인 봇에는 즉시 적용됩니다" if applied
                else "다음 실행부터 적용됩니다")
        if removed:
            note += f" — 주의: {', '.join(removed)} 한도가 해제되었습니다 (무제한)"
            log.warning("일일 한도 해제: %s — 다시 설정하기 전까지 무제한입니다",
                        ", ".join(removed))
        return {"saved": True, "applied_now": applied,
                "updated": updated, "removed": removed, "note": note}

    @app.get("/api/limits")
    async def limits_get(_=Depends(require_token)):
        if state.trader is not None:
            return state.trader.engine.budget.status()
        return {
            "running": False,
            "configured": {
                "max_daily_notional": float(os.environ.get("QUANT_LIMIT_DAILY_NOTIONAL", 0) or 0),
                "max_daily_orders": int(float(os.environ.get("QUANT_LIMIT_DAILY_ORDERS", 0) or 0)),
                "max_daily_loss": float(os.environ.get("QUANT_LIMIT_DAILY_LOSS", 0) or 0),
                "max_daily_loss_pct": float(os.environ.get("QUANT_LIMIT_DAILY_LOSS_PCT", 0) or 0),
            },
        }

    @app.post("/api/limits/release")
    async def limits_release(_=Depends(require_token)):
        """Clear a daily-cap halt for the rest of today. Deliberately explicit."""
        trader = _require_trader()
        trader.engine.budget.release()
        return trader.engine.budget.status()

    # ── 투자 성향 ────────────────────────────────────────────────────────
    def _profile_store() -> ProfileStore:
        return ProfileStore(os.environ.get("QUANT_PROFILE_FILE",
                                           "investor_profile.json"))

    @app.get("/api/profile/questions")
    async def profile_questions(_=Depends(require_token)):
        return {
            "questions": questionnaire(),
            "note": "여덟 문항으로 위험 감내도를 정확히 잴 수는 없습니다. "
                    "이건 진단이 아니라 출발점이고, 언제든 마이페이지에서 바꿀 수 "
                    "있습니다. 마지막 문항만은 성향이 아니라 사실을 묻는 것입니다.",
        }

    @app.get("/api/profile")
    async def profile_get(_=Depends(require_token)):
        return _profile_store().load().to_dict()

    @app.post("/api/profile")
    async def profile_save(req: ProfileRequest, _=Depends(require_token)):
        profile = score_answers(req.answers)
        if not profile.answers:
            raise HTTPException(400, "인식된 답안이 없습니다")
        store = _profile_store()
        store.save(profile)
        applied = _apply_profile_live(profile)
        return {**profile.to_dict(), "applied_now": applied}

    @app.patch("/api/profile")
    async def profile_override(req: ProfileOverrideRequest, _=Depends(require_token)):
        """마이페이지에서 축을 직접 밀어 올리거나 내린다."""
        store = _profile_store()
        profile = store.load()
        for axis, value in req.overrides.items():
            axis = axis.upper()
            if axis not in ("R", "H", "E", "C"):
                raise HTTPException(400, f"알 수 없는 축: {axis}")
            profile.overrides[axis] = max(-1.0, min(1.0, float(value)))
        store.save(profile)
        applied = _apply_profile_live(profile)
        return {**profile.to_dict(), "applied_now": applied}

    @app.delete("/api/profile/override/{axis}")
    async def profile_clear_override(axis: str, _=Depends(require_token)):
        store = _profile_store()
        profile = store.load()
        profile.overrides.pop(axis.upper(), None)
        store.save(profile)
        return profile.to_dict()

    def _apply_profile_live(profile) -> dict | None:
        """실행 중인 봇에 즉시 반영할 수 있는 것만 반영한다.

        사이즈·손절·한도는 바로 바뀌지만 봉 주기나 알파 구성은 바뀌지 않습니다 —
        그건 엔진을 다시 세워야 하는 일이라, 반쯤 바뀐 상태로 돌리는 것보다
        재시작이 필요하다고 말하는 편이 정직합니다.
        """
        if state.trader is None:
            return None
        settings = profile.settings()
        engine = state.trader.engine
        pm = engine.portfolio_model
        pm.max_position_weight = settings["max_position_weight"]
        pm.max_gross_leverage = settings["max_gross_leverage"]
        pm.cash_reserve_pct = settings["cash_reserve_pct"]
        if hasattr(pm, "target_vol"):
            pm.target_vol = settings["target_annual_vol"]
        budget = engine.budget
        budget.max_loss_pct = settings["max_daily_loss_pct"]
        budget.max_orders = settings["max_daily_orders"]
        for model in engine.risk.models:
            if model.name == "max_dd_per_security":
                model.atr_multiple = settings["stop_atr_multiple"]
                model.limit = settings["stop_ceiling_pct"]
            elif model.name == "trailing_stop":
                model.atr_multiple = settings["trailing_atr_multiple"]
            elif model.name == "max_positions":
                model.max_positions = settings["max_positions"]
        return {
            "sizing_and_risk": "즉시 적용됨",
            "needs_restart": ["봉 주기", "알파 모델 구성", "AI 데스크 사용 여부"],
        }

    # ── 초기 설정 ────────────────────────────────────────────────────────
    @app.get("/api/setup")
    async def setup_status(_=Depends(require_token)):
        store = CredentialStore(os.environ.get("QUANT_ENV_FILE", ".env"))
        return {
            "state": store.state().to_dict(),
            "venues": venue_catalog(),
            "operator_fields": [{"env": e, "label": l, "required": r}
                                for e, l, r in OPERATOR_FIELDS],
            "configured_keys": {k: v for k, v in store.redacted().items()},
        }

    @app.post("/api/setup")
    async def setup_save(req: SetupRequest, _=Depends(require_token)):
        """Store setup values. Only the keys this screen owns are accepted.

        Anything else — a typo, or a `HTTPS_PROXY` that would reroute the next
        broker call — is refused and named in `rejected`.
        """
        store = CredentialStore(os.environ.get("QUANT_ENV_FILE", ".env"))
        report = store.update(req.values)
        note = "저장된 값은 다시 조회할 수 없습니다 (재발급만 가능)"
        if report.rejected:
            note += (" — 거부된 항목: "
                     + ", ".join(f"{k}({v})" for k, v in report.rejected.items()))
        return {"written": report.written, "rejected": report.rejected,
                "state": store.state().to_dict(), "note": note}

    @app.post("/api/setup/verify/{venue_id}")
    async def setup_verify(venue_id: str, _=Depends(require_token)):
        if venue_id not in VENUES_BY_ID:
            raise HTTPException(404, f"알 수 없는 거래소: {venue_id}")
        store = CredentialStore(os.environ.get("QUANT_ENV_FILE", ".env"))
        return await store.verify(venue_id)

    @app.get("/api/models")
    async def models(_=Depends(require_token)):
        from quant.execution.models import BUILTIN_EXECUTION_MODELS
        from quant.portfolio.models import BUILTIN_PORTFOLIO_MODELS
        from quant.risk.models import BUILTIN_RISK_MODELS
        from quant.risk.protections import BUILTIN_PROTECTIONS
        from quant.strategy.builder import BUILTIN_ALPHA_MODELS

        return {
            "alpha": sorted(BUILTIN_ALPHA_MODELS) + ["council"],
            "portfolio": sorted(BUILTIN_PORTFOLIO_MODELS),
            "risk": sorted(BUILTIN_RISK_MODELS),
            "protections": sorted(BUILTIN_PROTECTIONS),
            "execution": sorted(BUILTIN_EXECUTION_MODELS),
        }

    # ── actions ──────────────────────────────────────────────────────────
    @app.post("/api/backtest")
    async def backtest(req: BacktestRequest, _=Depends(require_token)):
        from quant.backtest.runner import run_backtest

        if req.config_path:
            cfg = load_config(req.config_path)
        elif req.config:
            cfg = StrategyConfig.model_validate(req.config)
        elif state.config:
            cfg = state.config.model_copy(deep=True)
        else:
            raise HTTPException(400, "supply config_path or config")
        cfg.mode = RunMode.BACKTEST
        if req.start:
            cfg.backtest.start = datetime.fromisoformat(req.start.replace("Z", "+00:00"))
        if req.end:
            cfg.backtest.end = datetime.fromisoformat(req.end.replace("Z", "+00:00"))
        if req.starting_cash:
            cfg.portfolio.starting_cash = req.starting_cash
        try:
            result = await run_backtest(cfg)
        except Exception as exc:
            raise HTTPException(400, f"backtest failed: {exc}") from exc
        return result.to_dict()

    @app.post("/api/trader/start")
    async def start_trader(req: StartRequest, _=Depends(require_token)):
        """Start a dry-run or live trader from a config file on this machine.

        `mode: "live"` carries exactly the preconditions `quant live` does: the
        config file must itself declare `mode: live`, `broker.live_trading_confirmed`
        must be true, at least one daily cap must be set under `limits:`, and
        `confirm` must repeat the strategy name — the API's stand-in for the
        name the CLI makes a human type.
        """
        from quant.live.trader import LiveTrader

        if state.trader is not None and state.trader.running:
            raise HTTPException(409, "a trader is already running")
        try:
            cfg = load_config(req.config_path)
        except Exception as exc:
            raise HTTPException(400, f"설정을 불러오지 못했습니다: {exc}") from exc
        mode = RunMode(req.mode)
        if mode is RunMode.LIVE:
            assert_live_start_allowed(cfg, req.confirm)
        # 모드를 바꿨으면 스키마 검증을 다시 돌린다. 대입만으로는 돌지 않아서
        # 하루 한도 없는 실거래·paper 브로커 실거래가 그대로 통과했습니다.
        try:
            cfg = StrategyConfig.model_validate({**cfg.model_dump(), "mode": mode})
        except ValidationError as exc:
            raise HTTPException(
                400, f"이 설정으로는 시작할 수 없습니다: {exc.errors()[0]['msg']}"
            ) from exc
        trader = LiveTrader(cfg, state.state_path)
        trader.engine.ctx.bus.on(None, state.hub.publish)
        state.trader, state.config = trader, cfg
        state.trader_task = asyncio.create_task(trader.run())
        return {"started": True, "strategy": cfg.name, "mode": cfg.mode.value}

    @app.post("/api/trader/stop")
    async def stop_trader(_=Depends(require_token)):
        if state.trader is None:
            raise HTTPException(404, "no trader running")
        state.trader.running = False
        return {"stopping": True,
                "note": "the current cycle finishes first; open positions are left as-is"}

    @app.post("/api/trader/sync")
    async def sync_positions(_=Depends(require_token)):
        if state.trader is None:
            raise HTTPException(404, "no trader running")
        return await state.trader.engine.brokerage.sync()

    # ── stream ───────────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def stream(ws: WebSocket):
        if token:
            supplied = ws.query_params.get("token", "")
            if not secrets.compare_digest(supplied, token):
                await ws.close(code=4401)
                return
        await ws.accept()
        state.hub.clients.add(ws)
        try:
            for item in state.hub.recent(50):
                await ws.send_text(json.dumps(item, default=str))
            while True:
                await ws.receive_text()          # keep-alive / client pings
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            state.hub.clients.discard(ws)

    # ── dashboard ────────────────────────────────────────────────────────
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):
        log.exception("unhandled API error")
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


_SECRET_HINTS = ("key", "secret", "token", "password", "passphrase")


def _redact(node: Any) -> Any:
    """Never hand a credential back through the API, even to an authed caller."""
    if isinstance(node, dict):
        return {
            k: ("***" if any(h in k.lower() for h in _SECRET_HINTS) and v else _redact(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact(v) for v in node]
    return node
