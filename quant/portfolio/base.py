"""Portfolio construction — turning opinions into a target book.

This is where account size, leverage, concentration limits and cash reserves
live. Alpha models cannot reach in here, and this layer never second-guesses
direction — it only decides *how much*.

Every model returns absolute targets for the full union of (symbols with active
insights) ∪ (symbols currently held), so a symbol that stops being liked gets an
explicit zero target rather than being silently forgotten.

That rule reads *absence* as an instruction, and it only holds when the alpha
layer actually ran. When a model crashed, absence means nobody looked — so a
held name with no view of any kind is left alone rather than sold. An active
insight worth zero is still a decision and is still obeyed.

Sizing is gated by an asymmetric buy/hold band shared by every model: it takes
more conviction to open a position than to keep one. A signal oscillating across
a single threshold otherwise buys and sells the same name repeatedly while
carrying no new information, and every one of those round trips pays the 거래세
on its sell side. The band can only ever subtract exposure, never add it, which
is what keeps it out of the way of the risk layer downstream.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from quant.core.context import Context
from quant.core.types import Direction, Insight, PortfolioTarget, Symbol

log = logging.getLogger("quant.portfolio")


class PortfolioConstructionModel(ABC):
    name = "portfolio"

    def __init__(
        self,
        max_position_weight: float = 0.25,
        max_gross_leverage: float = 1.0,
        cash_reserve_pct: float = 0.02,
        allow_short: bool = False,
        min_trade_weight: float = 0.005,
        entry_conviction: float = 0.40,
        hold_conviction: float = 0.15,
    ):
        self.max_position_weight = max_position_weight
        self.max_gross_leverage = max_gross_leverage
        self.cash_reserve_pct = cash_reserve_pct
        self.allow_short = allow_short
        #: rebalances smaller than this fraction of equity are skipped — churning
        #: 0.1% of the book back and forth just donates the spread to the venue
        self.min_trade_weight = min_trade_weight
        if not 0.0 <= hold_conviction <= entry_conviction <= 1.0:
            raise ValueError(
                "buy/hold band needs 0 <= hold_conviction <= entry_conviction <= 1, got "
                f"hold={hold_conviction} entry={entry_conviction}"
            )
        #: the buy/hold spread, in `Insight.confidence` units. Opening asks for
        #: `entry_conviction`, keeping only `hold_conviction`; equal values turn
        #: the asymmetry off and 0/0 removes the gate entirely.
        self.entry_conviction = entry_conviction
        self.hold_conviction = hold_conviction

    @abstractmethod
    def weights(self, ctx: Context, insights: list[Insight]) -> dict[str, float]:
        """Return desired signed portfolio weights keyed by `Symbol.key`."""

    # ── shared pipeline ──────────────────────────────────────────────────
    def create_targets(self, ctx: Context, insights: list[Insight]) -> list[PortfolioTarget]:
        active = [i for i in insights if i.is_active(ctx.now)]
        raw = self.weights(ctx, active) if active else {}
        raw = self._apply_constraints(raw)

        symbols: dict[str, Symbol] = {i.symbol.key: i.symbol for i in active}
        for pos in ctx.portfolio.open_positions:
            symbols.setdefault(pos.symbol.key, pos.symbol)

        investable = ctx.equity * (1.0 - self.cash_reserve_pct)
        # 알파 층이 이번 봉을 다 보지 못했으면, 인사이트가 **없다** 는 사실이
        # 더 이상 "이 종목은 이제 아니다" 를 뜻하지 않습니다. 본 적이 없다는
        # 뜻입니다. 위 독스트링의 규칙(없으면 0 타깃 = 청산)은 알파가 온전히
        # 돌았을 때만 성립합니다.
        degraded = bool(ctx.state("alpha").get("degraded"))
        # **본 적 없는 것** 만 붙잡습니다. 활성 인사이트가 있는데 가중치가 0 이면
        # 그건 침묵이 아니라 판단입니다 — FLAT 거부권이거나 확신이 유지 문턱
        # 아래로 내려간 경우이고, 둘 다 나가라는 지시입니다. 알파가 불완전하다는
        # 이유로 그것까지 미루면 손절 성격의 청산을 막게 됩니다.
        viewed = {i.symbol.key for i in active}
        held_keys = {p.symbol.key for p in ctx.portfolio.open_positions}
        targets: list[PortfolioTarget] = []
        for key, symbol in symbols.items():
            if ctx.is_pinned(symbol):
                # The operator owns this one. Emitting no target at all (rather
                # than a zero) leaves the position untouched instead of closing it.
                continue
            weight = raw.get(key, 0.0)
            if degraded and key not in viewed and key in held_keys:
                # 핀과 같은 처리 — 타깃을 아예 내지 않으면 보유는 그대로 남습니다.
                # 리스크 층은 그대로 돌므로 손절과 트레일링은 계속 작동합니다.
                log.warning("%s: 알파가 이번 봉을 다 보지 못해 청산 판단을 미룹니다",
                            symbol.ticker)
                continue
            price = ctx.price(symbol)
            if price <= 0:
                continue
            current = ctx.portfolio.quantity(symbol)
            desired = Decimal("0") if weight == 0 else symbol.round_qty(
                Decimal(str(weight * investable / price))
            )
            # deadband: ignore rebalances too small to be worth the round trip
            delta_weight = abs(float(desired - current)) * price / max(ctx.equity, 1e-9)
            if desired != 0 and current != 0 and delta_weight < self.min_trade_weight:
                continue
            targets.append(PortfolioTarget(
                symbol, desired, tag=f"w={weight:+.4f}", source=self.name
            ))
        return targets

    def _apply_constraints(self, weights: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, w in weights.items():
            if not self.allow_short and w < 0:
                w = 0.0
            w = max(-self.max_position_weight, min(self.max_position_weight, w))
            if abs(w) < 1e-6:
                w = 0.0
            out[key] = w
        gross = sum(abs(w) for w in out.values())
        if gross > self.max_gross_leverage and gross > 0:
            scale = self.max_gross_leverage / gross
            out = {k: v * scale for k, v in out.items()}
        return out

    # ── helper for subclasses ────────────────────────────────────────────
    def _net_scores(self, insights: list[Insight], ctx: Context) -> dict[str, tuple[Symbol, float]]:
        """Collapse many insights per symbol into one decayed, signed score.

        FLAT is treated as a hard veto rather than a vote, so a regime filter or
        a risk-committee veto reliably zeroes the position instead of being
        averaged away by two enthusiastic momentum models. The veto is settled
        before the buy/hold band ever runs: a regime filter is a risk
        instruction wearing an alpha's clothes, and banding it would widen a
        stop the operator believes is in place.
        """
        buckets: dict[str, list[Insight]] = {}
        for ins in insights:
            buckets.setdefault(ins.symbol.key, []).append(ins)

        out: dict[str, tuple[Symbol, float]] = {}
        for key, items in buckets.items():
            symbol = items[0].symbol
            if any(i.direction is Direction.FLAT for i in items):
                out[key] = (symbol, 0.0)
                continue
            score, conviction = self._consensus(items, ctx.now)
            out[key] = (symbol, self._banded(ctx, symbol, score, conviction))
        return out

    @staticmethod
    def _consensus(items: list[Insight], now: datetime) -> tuple[float, float]:
        """One symbol's insights as (signed direction consensus, conviction).

        The consensus is a vote on *direction*: with every model pointing the
        same way it is exactly ±1 however timid they are, so on its own it says
        nothing about how strongly the book believes. Conviction is what does —
        the decayed confidence behind that vote, on the same 0..1 scale as
        `Insight.confidence`, falling both when models contradict each other and
        when their evidence goes stale. Magnitude reweights the vote but cancels
        out of the conviction, so a band expressed in confidence units means the
        same thing for a 1% and for a 20% expected move.
        """
        num = den = mass = 0.0
        for ins in items:
            scale = 1.0 + abs(ins.magnitude or 0.0)
            w = ins.decayed_confidence(now) * scale
            num += int(ins.direction) * w
            den += w
            mass += scale
        if den <= 0 or mass <= 0:
            return 0.0, 0.0
        return num / den, abs(num) / mass

    def _banded(self, ctx: Context, symbol: Symbol, score: float, conviction: float) -> float:
        """Asymmetric buy/hold spread — a higher bar to open than to keep.

        Novy-Marx & Velikov (RFS 29(1) 2016) test the simple cost-mitigation
        techniques against each other and find the buy/hold spread the most
        effective of them; Chen & Velikov's 120-anomaly replication puts the
        gain at roughly 7-15bp a month. It is worth more on KRX than in their US
        sample, because a Korean sell pays 거래세 with no offset, so every extra
        round trip is charged for information the signal never added.

        The band is measured against conviction rather than the score, since a
        book whose models all agree scores ±1 no matter how faint the evidence,
        and gating that would gate nothing.

        A sign flip is an open, not a hold: an existing long does not vouch for
        a short. So the position falls to zero unless the new direction clears
        the entry bar on its own.

        The result is the score or it is zero — never larger, never the other
        way round. Anything the unbanded engine would have closed still closes,
        which is what makes the band safe to sit upstream of the risk layer.
        """
        # The tax is paid on a position, not on an intention — an entry order
        # still resting is free to abandon — so this reads the filled quantity
        # and not the projected one.
        held = ctx.portfolio.quantity(symbol)
        sustaining = (held > 0 and score > 0) or (held < 0 and score < 0)
        floor = self.hold_conviction if sustaining else self.entry_conviction
        if conviction >= floor:
            return score
        log.debug(
            "band: %s conviction %.3f below the %s bar %.3f — score %+.3f dropped",
            symbol.ticker, conviction, "hold" if sustaining else "entry", floor, score,
        )
        return 0.0
