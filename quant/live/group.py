"""그룹 트레이더 — 계좌 하나 위에서 에이전트 여럿을 굴린다.

`LiveTrader` 는 엔진 하나를 처음부터 끝까지 책임집니다. 워밍업하고, 상태를
복원하고, 증권사에 붙고, 봉마다 판단하고, 끝나면 마지막 상태를 적고 연결을
닫습니다. 그 전부가 "이 프로세스에 봇은 하나" 라는 전제 위에 있습니다.

에이전트가 넷이 되면 그중 **일부만** 넷이 됩니다. 나머지는 계좌의 것이라
여전히 하나여야 하고, 하나여야 할 것이 넷이 되는 순간이 곧 사고입니다:

  계좌에 하나 (그룹이 한다)          에이전트마다 (트레이더가 한다)
  ─────────────────────────────      ──────────────────────────────
  상태 DB 열기와 소유권 주장          워밍업 · 시세 이력
  증권사 연결과 종료                  실행 행(runs) 시작/재개
  계좌 자본의 진실과 배분             포지션·잠금·핀 복원
  계좌 단위 하루 한도                 엔진 시작 · 봉마다 판단
  미귀속 채택 · 합계 불변식           체결 정산 · 최종 상태 저장

**소유권과 종료가 가장 위험합니다.** `LiveTrader` 의 두 종료 경로는 모두
`state.close()` 를 무조건 부릅니다. 넷이 같은 저장소를 쥐고 있으면 먼저 끝난
하나가 — 워밍업에서 죽은 것이라도 — 나머지 셋의 DB 연결과 그룹 전체의 소유권
주장을 닫아 버립니다. 그래서 그룹이 저장소를 열고, 트레이더에게는
`StateStore.agent_view` 로 만든 시점을 건넵니다. 시점의 `close()` 는 아무것도
하지 않습니다.

**정지도 그룹 단위입니다.** 슬리브 원장이 계좌와 어긋나면 어느 에이전트가
틀렸는지 알 방법이 없으므로 전부 멈춥니다 — 자세한 것은
`quant.live.gateway.AccountGateway.check_invariant` 에 있습니다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.types import UTC, RunMode
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway, GroupHalted
from quant.live.limits import TradingBudget
from quant.live.state import StateStore
from quant.live.trader import LiveTrader

log = logging.getLogger("quant.live.group")

#: 에이전트 하나가 마지막 사이클과 상태 플러시를 끝내기를 기다리는 시간.
#: 넷을 한꺼번에 기다리므로 이 값은 그룹 전체의 여유입니다 — 하나씩 순서대로
#: 기다리면 마지막 에이전트의 몫이 앞사람들의 대기 시간만큼 줄어듭니다.
STOP_GRACE_SECONDS = 20.0


class GroupTrader:
    """에이전트 여럿과 계좌 하나. 실행 단위이자 정지 단위입니다."""

    def __init__(
        self,
        group: AgentGroup,
        configs: dict[str, Any],
        state_path: str,
        *,
        venue,
        master_budget: TradingBudget | None = None,
        base_currency: str = "KRW",
        allocation_quantum: str = "1",
        profile_paths: dict[str, str] | None = None,
        meters: dict[str, Any] | None = None,
        resume: bool = True,
    ):
        if set(configs) != set(group.ids):
            missing = sorted(set(group.ids) - set(configs))
            extra = sorted(set(configs) - set(group.ids))
            raise ValueError(
                f"에이전트와 설정이 맞지 않습니다 — 설정 없음: {missing}, "
                f"그룹에 없음: {extra}"
            )
        self.group = group
        self.configs = configs
        self.resume = resume

        # 상태 저장소는 **그룹이** 엽니다. 여기서 소유권 주장(flock + db_owner)이
        # 한 번 일어나고, 트레이더들은 그 위의 시점만 받습니다.
        self.state = StateStore(state_path)
        self.gateway = AccountGateway(
            group, venue, master_budget=master_budget,
            base_currency=base_currency, allocation_quantum=allocation_quantum,
            store=self.state,
        )

        self.traders: dict[str, LiveTrader] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.started_at: datetime | None = None
        self._stopped = False

        profile_paths = profile_paths or {}
        meters = meters or {}
        for spec in group.agents:
            self.traders[spec.agent_id] = self._build_trader(
                spec, profile_paths.get(spec.agent_id),
                meters.get(spec.agent_id))

    # ── 배선 ─────────────────────────────────────────────────────────────
    def _build_trader(self, spec: AgentSpec, profile_path, meter) -> LiveTrader:
        """에이전트 한 대. 슬리브와 상태 시점을 받아 세웁니다.

        브로커를 **생성자 인자로** 넘깁니다. 세운 뒤에 바꿔치기하면 슬리브의
        `budget` 과 `portfolio` 가 `None` 으로 남고 `ctx.brokerage` 는 계좌
        어댑터를 가리킨 채로 남습니다 — 슬리브가 존재하는 이유 전부를 우회하는
        길입니다.
        """
        config = self.configs[spec.agent_id]
        sleeve = SleeveBrokerage(
            spec.agent_id, self.gateway, mode=spec.mode,
            allow_short=bool(config.portfolio.allow_short),
        )
        trader = LiveTrader(
            config,
            resume=self.resume,
            profile_path=profile_path,
            meter=meter,
            brokerage=sleeve,
            state=self.state.agent_view(spec.agent_id),
        )
        # 청산 손익을 계좌 원장으로 잇습니다. 이것이 없으면 계좌 하루 손실
        # 한도의 실현손익이 영원히 0 이라 한도가 걸리지 않습니다.
        self.gateway.attach_engine(spec.agent_id, trader.engine)
        return trader

    # ── 수명주기 ─────────────────────────────────────────────────────────
    async def start(self) -> dict:
        """계좌 단위 준비를 한 번 하고, 에이전트들을 띄운다.

        순서가 중요합니다. 증권사 연결과 자본 배분과 미귀속 채택은 **어느
        에이전트도 주문을 내기 전에** 끝나야 합니다 — 배분 전에 사이징하면
        에이전트는 자기 몫이 0 인 줄 알고, 미귀속 채택 전에 불변식을 보면
        사용자가 앱에서 직접 산 주식이 드리프트로 읽혀 그룹이 즉사합니다.
        """
        # 계좌 원장을 **어느 에이전트도 주문을 내기 전에** 되살립니다. 이것이
        # 없으면 재시작이 계좌에 새 허용치를 줍니다 — 하루 손실 한도가 걸려
        # 멈춘 계좌를 재배포 한 번이 풀어 주는데, 그건 한도가 아니라 한도와
        # 초기화 버튼을 함께 둔 것이고 그 버튼은 봇이 고장 났을 때 가장 자주
        # 눌립니다.
        mode = (RunMode.LIVE.value if self.group.has_live
                else RunMode.DRY_RUN.value)
        self.state.restore_account_budget(self.gateway.master_budget, mode=mode)
        # **미귀속 채택보다 먼저** 슬리브 원장을 되살립니다. 순서가 뒤집히면
        # `adopt_unassigned` 가 빈 원장을 보고 계좌 전부를 미귀속으로 받아
        # 적고, 미귀속은 아무도 팔 수 없으므로 재시작 하나가 모든 보유를
        # 영구히 묶어 버립니다 — 그리고 합계 불변식은 그 상태를 정상으로
        # 읽으므로 아무도 알아채지 못합니다.
        self.gateway.adopt_sleeves(self.state.restore_sleeves())
        # 재시작 전에 낸 주문들의 주인도 함께 이어받습니다. 잃으면 그 체결이
        # 미귀속으로 떨어지고, 미귀속은 아무도 팔 수 없으므로 판 적도 없는
        # 주식이 손절도 청산도 닿지 않는 채로 계좌에 남습니다.
        self.gateway.adopt_order_agents(self.state.restore_order_agents())

        await self.gateway.connect()

        equity = await self._account_equity()
        if equity <= 0:
            # 잔고를 못 읽으면 시작하지 않습니다. 0 을 나눠 주면 에이전트들은
            # 자기 몫이 없는 줄 알고 아무것도 사지 않는데, 그보다 나쁜 것은
            # **비율 손실 한도가 조용히 죽는다** 는 점입니다 — 분모(시작 자산)가
            # 0 이면 `max_daily_loss_pct` 는 하루 종일 걸리지 않고, 복원된
            # 포지션은 그 사이에도 계속 움직입니다.
            raise GroupHalted(
                "계좌 잔고를 읽지 못해 그룹을 시작하지 않았습니다. 자본 배분의 "
                "분모이자 비율 손실 한도의 기준이라, 모르는 채로 시작하면 계좌 "
                "한도가 걸리지 않습니다. 증권사 연동을 확인한 뒤 다시 시작하세요."
            )
        self.gateway.allocate_capital(equity)
        # 비율 손실 한도의 분모. 오늘 원장이 이미 있으면 그 시작 자산을 그대로
        # 두어야 합니다 — 장중 재시작마다 분모를 지금 자산으로 갈아 끼우면,
        # 이미 20% 를 잃은 계좌가 "오늘은 아직 0%" 가 됩니다.
        ledger = self.gateway.master_budget.roll(equity=equity)
        if not ledger.starting_equity:
            ledger.starting_equity = equity
            self.state.save_account_budget(self.gateway.master_budget, mode)

        venue_positions = await self.gateway.venue.positions()
        self.gateway.adopt_unassigned(venue_positions)

        self.started_at = datetime.now(UTC)
        for agent_id, trader in self.traders.items():
            task = asyncio.create_task(trader.run(), name=f"agent-{agent_id}")
            task.add_done_callback(
                lambda t, a=agent_id: self._finished(a, t))
            self._tasks[agent_id] = task
        log.warning(
            "그룹 시작: %s (계좌 %.0f %s)",
            ", ".join(f"{s.label}({s.agent_id}) {s.capital_weight:.0%}"
                      for s in self.group.agents),
            equity, self.gateway.base_currency,
        )
        return self.status()

    async def _account_equity(self) -> float:
        """배분의 분모. 읽지 못하면 0 입니다 — 추정치로 주문을 내지 않습니다."""
        try:
            balances = await self.gateway.venue.balances()
        except Exception as exc:  # noqa: BLE001 — 배분 전이라 주문은 아직 없다
            log.error("계좌 잔고를 읽지 못해 자본을 배분하지 못했습니다: %s", exc)
            return 0.0
        return float(balances.get(self.gateway.base_currency, 0.0) or 0.0)

    def _finished(self, agent_id: str, task: asyncio.Task) -> None:
        """에이전트 하나가 끝났다 — 스스로 멈췄든, 죽었든.

        죽은 이유를 남기지 않으면 화면에는 "실행 중 아님" 만 보이고, 사용자는
        자기가 멈춘 줄 압니다. 워밍업 실패나 인증 거절은 아무도 모르는 채로
        지나갑니다.
        """
        if task.cancelled():
            self.errors[agent_id] = "취소됨"
            return
        exc = task.exception()
        if exc is None:
            return
        text = str(exc)[:300]
        speaks_korean = any("가" <= ch <= "힣" for ch in text[:40])
        self.errors[agent_id] = (
            text if speaks_korean else f"{type(exc).__name__}: {text}")
        log.error("에이전트 %s 중단: %s", agent_id, self.errors[agent_id])

    async def stop(self, wait: float = STOP_GRACE_SECONDS) -> dict:
        """전부에게 멈추라고 말한 뒤 **함께** 기다린다.

        하나씩 순서대로 기다리면 마지막 에이전트의 여유가 앞사람들의 대기
        시간만큼 줄어듭니다. 보유 포지션은 그대로 둡니다.
        """
        alive = [(a, t) for a, t in self._tasks.items() if not t.done()]
        for agent_id, _ in alive:
            self.traders[agent_id].request_stop()
        if alive and wait > 0:
            await asyncio.wait({t for _, t in alive}, timeout=wait)
        pending = [a for a, t in self._tasks.items() if not t.done()]
        if pending:
            log.warning("에이전트 %s 가 %.0f초 안에 멈추지 않았습니다 — "
                        "현재 사이클이 끝나면 종료됩니다", ", ".join(pending), wait)
        return {"stopping": True, "stopped": not pending, "pending": pending,
                "note": "현재 사이클을 마치고 멈춥니다. 보유 포지션은 그대로 둡니다."}

    async def shutdown(self, wait: float = STOP_GRACE_SECONDS) -> None:
        """계좌 단위 자원을 마지막에 **한 번** 놓는다.

        트레이더들이 각자 자기 마지막 상태를 적고 끝난 뒤에야 증권사 연결과 DB
        소유권을 놓습니다. 순서가 뒤집히면 아직 상태를 적는 중인 에이전트가 닫힌
        연결을 만납니다.
        """
        if self._stopped:
            return
        self._stopped = True
        await self.stop(wait)
        for agent_id, task in self._tasks.items():
            if not task.done():
                log.warning("에이전트 %s 의 현재 사이클을 취소합니다", agent_id)
                task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks.values()), timeout=5.0)

        with contextlib.suppress(Exception):
            await self.gateway.close()
        try:
            self.state.close()
        except Exception:  # noqa: BLE001 — best-effort final release
            log.exception("그룹 상태 저장소를 닫지 못했습니다")

    # ── 조회 ─────────────────────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return any(not t.done() for t in self._tasks.values())

    def trader(self, agent_id: str) -> LiveTrader | None:
        task = self._tasks.get(agent_id)
        if task is None or task.done():
            return None
        return self.traders.get(agent_id)

    def status(self) -> dict:
        """그룹 하나의 상태. 에이전트별 배열과 계좌 요약을 함께 답니다."""
        agents = []
        for spec in self.group.agents:
            trader = self.traders[spec.agent_id]
            task = self._tasks.get(spec.agent_id)
            running = task is not None and not task.done()
            row = {
                "agent_id": spec.agent_id,
                "label": spec.label,
                "mode": spec.mode.value,
                "capital_weight": spec.capital_weight,
                "allocated": self.gateway.sleeve_balances(spec.agent_id),
                "strategy": trader.config.name,
                "running": running,
                "error": self.errors.get(spec.agent_id, ""),
                "sleeve": {k: str(v) for k, v in
                           self.gateway.sleeve_positions(spec.agent_id).items()},
            }
            if running:
                with contextlib.suppress(Exception):
                    row["trader"] = trader.status()
            agents.append(row)
        return {
            "running": self.alive,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            # 계좌의 위험 등급 — **가장 위험한 에이전트가 정합니다.** 넷 중
            # 하나만 실거래여도 이 계좌에서는 진짜 주문이 나갑니다.
            #
            # 최상위에 두는 이유는 화면 때문입니다. 단일 봇 시절부터 있던 코드가
            # `status.mode` 를 읽어 "이것이 진짜 돈인가" 를 판정하는데, 그룹
            # 응답에 그 키가 없으면 그 판정이 실패하고 **수동 주문이 통째로
            # 막힙니다** — 안전이 아니라 고장으로 읽히는 종류의 실패입니다.
            "mode": (RunMode.LIVE.value if self.group.has_live
                     else RunMode.DRY_RUN.value),
            "account": self.gateway.status(),
            "agents": agents,
        }

    @property
    def has_live(self) -> bool:
        return self.group.has_live

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        return (f"<GroupTrader {len(self.group.agents)}개 "
                f"{'실거래' if self.has_live else '관찰'} "
                f"{'구동중' if self.alive else '정지'}>")


def paper_venue_for(group: AgentGroup, configs: dict, base_currency: str = "KRW"):
    """관찰 전용 그룹이 쓸 가상 증권사.

    `mode` 가 전부 `dry_run` 인 그룹에는 실제 어댑터를 물리지 않습니다. 물리면
    관찰만 하기로 한 주문이 진짜 계좌로 나갑니다 — 게이트웨이가 그것을 막고는
    있지만, 애초에 닿지 않게 하는 편이 방어선 하나를 아끼는 것보다 낫습니다.
    """
    from quant.brokerage.paper import PaperBrokerage
    from quant.core.account import Portfolio

    if group.has_live:
        raise ValueError(
            "실거래 에이전트가 있는 그룹에는 가상 증권사를 쓸 수 없습니다"
        )
    cash = sum(float(c.portfolio.starting_cash) for c in configs.values())
    return PaperBrokerage(Portfolio(cash, base_currency))


__all__ = ["GroupTrader", "STOP_GRACE_SECONDS", "paper_venue_for", "RunMode"]
