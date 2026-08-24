"""새 봉을 엔진에 넘기기 전에 거르는 자리 — 확정됐는가, 빠진 것은 없는가.

여기가 없으면 봇은 시세 소스가 주는 것을 그대로 믿습니다. REST 폴링에서는
그럭저럭 넘어갔습니다 — 마감된 봉만 오고, 한 번에 몇 개 밀리지도 않으니까.
그 "그럭저럭" 안에 조용한 구멍이 셋 있었습니다.

  1. **한 사이클에 새 봉이 여러 개면 가장 최근 것 하나만** 엔진에 갔습니다.
     나머지는 "봤다" 로 표시되고 버려집니다. 그러면 지표는 08-24 다음이
     08-27 인 시계열 위에서 돕니다 — 20봉 이동평균이 실제로는 23봉을 덮고,
     그게 틀렸다고 말해 주는 것은 아무 데도 없습니다.
  2. **조회 창(기본 3봉)보다 큰 구멍은 요청조차 하지 않았습니다.** 10분
     끊겼다 돌아온 1분봉 봇은 그 7봉을 영영 못 봅니다.
  3. **아직 만들어지는 중인 봉이 그 시각의 확정봉 자리를 차지했습니다.**
     나중에 오는 진짜 확정봉은 "이미 본 시각" 이라 버려집니다. 진행 중 봉의
     고가·저가·거래량은 확정값보다 언제나 좁으므로, 그 자리에 남은 것은
     틀린 봉입니다.

셋 다 REST 에서도 일어나지만 푸시 피드에서는 셋 다 **기본 동작**입니다 —
웹소켓은 진행 중인 봉을 계속 갱신해서 보내고, 끊겼다 붙습니다. 그래서 이
계층은 전송 방식을 모릅니다: 폴링이 붙든 소켓이 붙든 통과 조건은 같습니다.

**언제 이게 안 통하는가.** 달력을 모릅니다. 봉 사이가 벌어졌을 때 그것이
주말·휴장인지 우리가 놓친 것인지 이 코드는 구분하지 못하고, 구분하려 들지도
않습니다 — 대신 그 구간을 `history()` 로 되묻습니다. 거래소가 빈 목록을 주면
그 구간에는 장이 없었던 것이고, 되묻다 실패했을 때만 "이 구간은 못 봤다" 로
올립니다. 간격만 보고 구멍이라고 우기면 주말마다 거짓 경고가 나고, 사람은
곧 그 경고를 안 읽게 됩니다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from quant.core.types import Bar, Symbol, timeframe_delta, utcnow
from quant.data.provider import DataProvider

log = logging.getLogger("quant.data.feed")

#: 화면이 그대로 띄우는 문장입니다. "실시간" 배지는 거래소가 봉을 **밀어 줄
#: 때만** 참입니다 — 폴링으로 받아 놓고 실시간이라고 적으면, 화면에서 본
#: 가격과 주문이 닿는 가격이 다른 이유를 사용자가 영원히 알 수 없습니다.
MODE_KO = {
    "realtime": "실시간 — 거래소가 봉을 밀어 줍니다",
    "polled": "REST 폴링 — 봉은 마감된 뒤에야 갱신됩니다",
}


@dataclass(frozen=True)
class FeedGap:
    """되메우지 **못한** 구간. 메운 구간은 여기 남지 않습니다.

    남기는 기준이 "간격이 벌어졌다" 가 아니라 "확인하려다 실패했다" 인 것이
    핵심입니다. 앞의 기준으로 남기면 주말마다 한 건씩 쌓입니다.
    """

    ticker: str
    start: datetime
    end: datetime
    bars: int
    reason: str

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "from": self.start.isoformat(),
                "to": self.end.isoformat(), "bars": self.bars, "reason": self.reason}


class LiveBarFeed:
    """프로바이더가 주는 봉을 확정·연속·시간순으로 정리해서 내놓습니다.

    `pending()` 이 돌려주는 것은 "아직 엔진에 넘기지 않은 확정봉 전부" 이고,
    심볼이 섞인 채 시간순입니다. 심볼당 하나로 줄이지 않는 것이 요점입니다 —
    줄이는 순간 위 (1) 이 되돌아옵니다.

    푸시 전송(웹소켓)이 붙는 날에는 `pending()` 대신 소켓이 `admit()` 을
    부르면 됩니다. 확정 판정·중복 제거·구멍 되메우기는 그대로 쓰입니다.
    """

    def __init__(self, provider: DataProvider, timeframe: str, *,
                 poll_bars: int = 3, max_backfill_bars: int = 240,
                 gap_log: int = 20):
        self.provider = provider
        self.timeframe = timeframe
        #: 한 번에 몇 봉을 물어볼지. 3 이면 한두 사이클 늦어도 폴링만으로
        #: 따라잡히고, 그보다 크게 벌어진 것은 아래 되메우기가 맡습니다.
        self.poll_bars = poll_bars
        #: 되메우기 상한. 이걸 넘는 구멍은 메우지 않고 **알립니다** — 하루치
        #: 1분봉을 한꺼번에 밀어 넣으면 지표는 이어져도 그 위에서 나오는 판단은
        #: 이미 지나간 시장에 대한 것이고, 그 주문은 지금 가격에 나갑니다.
        self.max_backfill_bars = max_backfill_bars
        #: 심볼별 마지막 확정봉 시각. 구멍 판정의 기준이라 한 벌만 존재해야
        #: 합니다(트레이더는 이걸 빌려 씁니다).
        self.seen: dict[str, datetime] = {}
        self.backfilled = 0
        self.held_partial = 0
        self.gaps: list[FeedGap] = []
        self._gap_log = gap_log
        self._failed: list[str] = []

    # ── 전송 방식 ────────────────────────────────────────────────────────
    @property
    def mode(self) -> str:
        """`realtime` 은 거래소가 밀어 줄 때만입니다.

        프로바이더가 스스로 신고하는 값을 그대로 씁니다. 여기에 상수를 박아
        두면 폴백으로 내려간 날에도 화면은 계속 "실시간" 이라고 말합니다.
        """
        return "realtime" if getattr(self.provider, "supports_streaming", False) else "polled"

    # ── 받기 ─────────────────────────────────────────────────────────────
    async def pending(self, symbols: list[Symbol]) -> list[Bar]:
        """폴링 한 바퀴. 새 확정봉을 시간순으로 돌려줍니다.

        한 종목이 실패해도 나머지는 나옵니다 — 시세가 안 오는 종목 하나 때문에
        들고 있는 다른 종목의 손절이 평가되지 않으면 그게 훨씬 비쌉니다.
        """
        now = utcnow()
        results = await asyncio.gather(
            *(self.provider.latest_bars(s, self.timeframe, self.poll_bars)
              for s in symbols),
            return_exceptions=True,
        )
        out: list[Bar] = []
        failed: list[str] = []
        for symbol, result in zip(symbols, results):
            if isinstance(result, BaseException):
                failed.append(symbol.ticker)
                log.warning("data fetch failed for %s: %s", symbol.ticker, result)
                continue
            out.extend(await self.admit(symbol, result, now=now))
        self._failed = failed
        out.sort(key=lambda b: b.ts)
        return out

    async def admit(self, symbol: Symbol, bars: list[Bar], *,
                    now: datetime | None = None) -> list[Bar]:
        """이 종목이 준 봉들 중 엔진이 봐야 할 것만, 시간순으로.

        진행 중인 봉은 **버리지 않고 그냥 넘어갑니다**. `seen` 을 올리지 않으니
        다음 번에 확정본이 오면 그때 통과합니다 — 버리고 표시까지 해 버리면
        그 시각의 봉은 영영 진행 중이던 모습으로 남습니다.
        """
        now = now or utcnow()
        last = self.seen.get(symbol.key)
        fresh: dict[datetime, Bar] = {}
        for bar in sorted(bars, key=lambda b: b.ts):
            if bar.end_ts > now:
                self.held_partial += 1
                continue
            if last is not None and bar.ts <= last:
                continue
            # 같은 시각이 두 번 오면 나중 것을 씁니다 — 갱신본이 확정값에
            # 더 가깝습니다.
            fresh[bar.ts] = bar
        if not fresh:
            return []
        ordered = [fresh[ts] for ts in sorted(fresh)]
        filled: list[Bar] = []
        if last is not None:
            filled = await self._backfill(symbol, last, ordered[0].ts, now)
        self.seen[symbol.key] = ordered[-1].ts
        return filled + ordered

    async def _backfill(self, symbol: Symbol, last: datetime,
                        next_ts: datetime, now: datetime) -> list[Bar]:
        """`last` 와 `next_ts` 사이를 `history()` 로 되묻습니다.

        빈 목록이 오면 그 구간에는 장이 없었던 것으로 봅니다 — 여기서 달력을
        따로 들지 않는 이유입니다. 되묻는 것 자체가 실패했을 때만 못 본 구간으로
        올립니다.
        """
        step = timeframe_delta(self.timeframe)
        missing = int((next_ts - last) / step) - 1
        if missing <= 0:
            return []
        if missing > self.max_backfill_bars:
            self._record_gap(symbol, last + step, next_ts - step, missing,
                             f"되메우기 한도 {self.max_backfill_bars}봉을 넘습니다")
            return []
        try:
            older = await self.provider.history(symbol, self.timeframe,
                                                last + step, next_ts)
        except Exception as exc:            # noqa: BLE001 — 못 본 구간으로 올립니다
            self._record_gap(symbol, last + step, next_ts - step, missing, str(exc))
            return []
        got = [b for b in sorted(older, key=lambda b: b.ts)
               if last < b.ts < next_ts and b.end_ts <= now]
        if got:
            self.backfilled += len(got)
            log.info("%s: 못 본 구간 %d봉을 REST 로 메웠습니다", symbol.ticker, len(got))
        return got

    def _record_gap(self, symbol: Symbol, start: datetime, end: datetime,
                    bars: int, reason: str) -> None:
        log.warning("%s: %s ~ %s (%d봉) 을 못 봤습니다 — %s. 이 구간의 지표는 "
                    "이어 붙인 것이고, 그 위의 판단은 그만큼 덜 믿을 수 있습니다.",
                    symbol.ticker, start.isoformat(), end.isoformat(), bars, reason)
        self.gaps.append(FeedGap(symbol.ticker, start, end, bars, reason))
        if len(self.gaps) > self._gap_log:
            del self.gaps[: len(self.gaps) - self._gap_log]

    # ── 상태 ─────────────────────────────────────────────────────────────
    def health(self) -> dict:
        """화면과 로그가 읽는 시세 상태. 모르면 비웁니다, 지어내지 않습니다."""
        return {
            "mode": self.mode,
            "mode_ko": MODE_KO[self.mode],
            "provider": getattr(self.provider, "name", ""),
            "timeframe": self.timeframe,
            "backfilled_bars": self.backfilled,
            "held_partial_bars": self.held_partial,
            # 못 본 구간. 비어 있으면 "구멍이 없다" 가 아니라 "못 봤다고
            # 판정된 것이 없다" 입니다 — 위 docstring 의 한계 그대로입니다.
            "unseen_windows": [g.to_dict() for g in self.gaps],
            "fetch_failures": list(self._failed),
            "degraded": bool(self.gaps or self._failed),
        }
