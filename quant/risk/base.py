"""Risk management — the last layer that can change a target before execution.

Risk models see the *proposed* book and may only ever reduce exposure: cut a
target, flatten a position, or veto an entry. They cannot open something the
alpha and portfolio layers did not ask for. That one-way constraint is what
makes a stack of risk models safe to compose in any order.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal

from quant.core.context import Context
from quant.core.types import PortfolioTarget, one_line_error

log = logging.getLogger("quant.risk")


class RiskManagementModel(ABC):
    name = "risk"

    @abstractmethod
    def manage(self, ctx: Context, targets: list[PortfolioTarget]) -> list[PortfolioTarget]:
        """Return the adjusted target book."""

    def on_trade_closed(self, ctx: Context, trade) -> None:
        return None

    # -- durable state ---------------------------------------------------
    def state_payload(self) -> dict | None:
        """State that must survive a restart, or None when there is none.

        A halt a model imposed on itself is the case this exists for: the lock
        it set is already persisted, but the counter that decides whether the
        next one is permanent is not, so a restart hands a broken strategy a
        fresh set of lives.
        """
        return None

    def load_state(self, payload: dict) -> None:
        return None

    # -- helper ----------------------------------------------------------
    @staticmethod
    def _flatten(target: PortfolioTarget, reason: str) -> PortfolioTarget:
        return PortfolioTarget(target.symbol, Decimal("0"), tag=reason, source="risk")


class CompositeRiskModel(RiskManagementModel):
    """Applies each model in order; the most restrictive result survives.

    A model that raises has not looked at this book. Leaving the targets
    unchanged and carrying on says the opposite — that it looked and asked for
    no reduction — and this is the last layer before the broker, so what goes
    out is the unreduced size the alpha asked for.

    So a failure holds exposure flat for the bar: every reduction still goes
    through, and nothing that adds is sent. That is the one thing we can say
    safely without knowing what the model would have cut. A bar of delay costs
    a late entry; an unvetted increase costs the position the model existed to
    refuse. The models that failed are named on the targets they held back, so
    the reason travels with the order tag to whoever is watching.
    """

    name = "composite_risk"

    def __init__(self, *models: RiskManagementModel):
        self.models = list(models)
        #: 이번 봉에 판정을 못 한 모델들. `manage` 호출마다 새로 씁니다.
        self.failed: list[str] = []

    def manage(self, ctx, targets):
        self.failed = []
        for model in self.models:
            try:
                targets = model.manage(ctx, targets)
            except Exception as exc:
                self.failed.append(f"{model.name}: {one_line_error(exc)}")
                log.exception("리스크 모델 %s 이(가) 이번 봉을 판정하지 못했습니다",
                              model.name)
        if not self.failed:
            return targets
        why = "리스크 판정 실패 — " + "; ".join(self.failed)
        log.error("%s — 줄이는 주문만 내보냅니다", why)
        return [self._capped(ctx, t, why) for t in targets]

    def _capped(self, ctx: Context, t: PortfolioTarget, why: str) -> PortfolioTarget:
        """Same shape as `TradingLockGate`: close freely, never add."""
        current = ctx.portfolio.quantity(t.symbol)
        if t.quantity == 0:
            return t                                     # 청산은 언제나 통과
        if current == 0:
            return PortfolioTarget(t.symbol, Decimal("0"),
                                   tag=f"진입 보류: {why}", source=self.name)
        if (t.quantity > 0) != (current > 0):
            # 전환은 청산 + 신규다. 청산 쪽만 허용한다.
            return PortfolioTarget(t.symbol, Decimal("0"),
                                   tag=f"전환 보류, 청산만: {why}", source=self.name)
        if abs(t.quantity) > abs(current):
            return PortfolioTarget(t.symbol, current,
                                   tag=f"증액 보류: {why}", source=self.name)
        return t                                         # 줄이는 주문

    # -- durable state ---------------------------------------------------
    def durable_state(self) -> dict[str, dict]:
        """What each model must not lose to a restart, keyed by model name."""
        out: dict[str, dict] = {}
        for model in self.models:
            payload = model.state_payload()
            if payload is not None:
                out[model.name] = payload
        return out

    def load_durable_state(self, saved: dict[str, dict]) -> int:
        restored = 0
        for model in self.models:
            payload = saved.get(model.name)
            if payload is None:
                continue
            try:
                model.load_state(payload)
            except Exception:
                log.exception("리스크 모델 %s 상태를 복원하지 못했습니다", model.name)
                continue
            restored += 1
        return restored

    def on_trade_closed(self, ctx, trade):
        for m in self.models:
            m.on_trade_closed(ctx, trade)
