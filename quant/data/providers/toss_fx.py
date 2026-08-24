"""토스 환율 소스 — `GET /api/v1/exchange-rate`.

공식 스펙에 `baseCurrency`·`quoteCurrency` (둘 다 필수) 와 **`dateTime`(선택)**
이 있습니다. 마지막 것이 이 계층 전체의 근거입니다 — 그게 없었다면 "봉 시각의
환율" 은 쓸 수 없는 규칙이었을 것입니다.

**이름이 뒤집혀 있습니다.** 토스의 `baseCurrency` 는 환산의 **출발**(1 단위를
세는 쪽), `quoteCurrency` 는 **도착**(얼마인지를 세는 쪽)입니다. 그런데 이
저장소에서 `Symbol.quote_currency` 는 종목이 매겨진 통화, 즉 환산의 출발입니다.
같은 단어가 정확히 반대 역할을 가리키므로, `Fx` 는 그 단어를 아예 쓰지 않고
`source`/`target` 으로 넘깁니다. 여기서 다시 토스 이름으로 되돌립니다 —
**뒤집기가 일어나는 곳은 이 함수 한 줄뿐입니다.** 두 곳에서 뒤집으면 어느 날
한 곳만 고치게 되고, 원달러가 1,380배 또는 1/1,380배 틀린 채로 화면에 뜹니다.

`rate` 와 `midRate` 중 **`midRate`(매매기준율)** 를 씁니다. `rate` 는 매수
환율이라 스프레드가 붙어 있어서, 그걸로 평가하면 달러 종목을 사자마자
평가금액이 스프레드만큼 부풀고 팔 때 그만큼 사라집니다 — 전략이 하지 않은
매매의 손익이 곡선에 섞이는 것입니다. 대신 이 선택 때문에 **실제 환전
스프레드는 비용으로 물리지 않습니다**: 외화 종목의 실현손익은 그 폭만큼
낙관적이고, 이 계층은 그 폭을 알지 못합니다.
"""
from __future__ import annotations

import logging
from datetime import datetime

from quant.brokerage.toss_broker import _FIELDS, TossProvider
from quant.core.fx import FxRate, FxRateSource, FxUnavailable, register_fx_source
from quant.core.types import UTC

log = logging.getLogger("quant.data.toss_fx")


def _num(raw) -> float | None:
    """스펙의 decimal **문자열** → 수. 숫자가 아니면 None.

    0.0 으로 물러서지 않습니다 — 환율 0 은 외화 평가금액을 통째로 지우면서도
    화면에는 정상적인 숫자로 뜹니다.
    """
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_ts(raw) -> datetime | None:
    """`2026-03-25T09:30:00+09:00` → tz-aware UTC. 못 읽으면 None."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(UTC)


@register_fx_source("toss")
class TossFxSource(FxRateSource):
    """토스 Open API 의 표시 환율. 스펙상 KRW↔USD 뿐입니다."""

    name = "toss_fx"

    def __init__(self, client_id: str = "", client_secret: str = "", **kwargs):
        # 토큰 발급·캐시·레이트리밋·`{"result": ...}` 벗기기는 전부 브로커가 쓰는
        # 클라이언트에 이미 있습니다. 복제하면 토큰 캐시가 둘이 되고, 그때부터
        # 발급 호출이 두 배가 됩니다 — `toss_flow` 와 같은 이유로 그대로 씁니다.
        self._data = TossProvider(client_id, client_secret, **kwargs)
        self._client = self._data.client

    async def rate(self, source: str, target: str, when: datetime) -> FxRate:
        params = {
            # ── 이름 뒤집기는 여기 두 줄이 전부입니다 ──
            "baseCurrency": source,      # 1 단위를 세는 쪽 = 환산의 출발
            "quoteCurrency": target,     # 얼마인지를 세는 쪽 = 환산의 도착
            # 안 보내면 "지금" 이 옵니다. 그러면 어제 체결이 오늘 환율로
            # 환산되어 손익에 환차익이 섞입니다 — 이 인자가 이 계층의 전부라
            # 절대 생략하지 않습니다.
            "dateTime": when.isoformat(),
        }
        data = await self._client.request(
            "GET", _FIELDS["exchange_rate_path"], params=params)
        if not isinstance(data, dict):
            raise FxUnavailable(f"토스 환율 응답을 읽을 수 없습니다: {str(data)[:200]}")

        # 응답이 우리가 물어본 쌍을 그대로 되돌려 주는지 확인합니다. 뒤집힌 쌍을
        # 그냥 쓰면 1,380 대신 1/1,380 을 곱하게 되는데, 그 결과도 화면에는
        # 평범한 숫자로 뜹니다.
        got = ((data.get("baseCurrency") or source).upper(),
               (data.get("quoteCurrency") or target).upper())
        if got != (source.upper(), target.upper()):
            raise FxUnavailable(
                f"토스 환율 응답의 통화 쌍이 다릅니다 — 요청 {source}→{target}, "
                f"응답 {got[0]}→{got[1]}"
            )

        rate = _num(data.get("midRate"))
        if rate is None:
            # 스펙상 midRate 는 필수라 여기 오면 스펙이 바뀐 것입니다. 매수
            # 환율이라도 없는 것보다는 낫지만, 뜻이 다른 값이라 조용히 쓰지는
            # 않습니다.
            rate = _num(data.get("rate"))
            if rate is not None:
                log.warning("토스 환율 응답에 midRate 가 없어 매수 환율(rate)을 "
                            "씁니다 — 평가금액에 환전 스프레드가 섞입니다")
        if rate is None:
            raise FxUnavailable(
                f"토스 환율 응답에 환율이 없습니다: {str(data)[:200]}")

        return FxRate(
            source=source.upper(), target=target.upper(), rate=rate, asof=when,
            valid_from=_parse_ts(data.get("validFrom")),
            valid_until=_parse_ts(data.get("validUntil")),
            origin=self.name,
        )

    async def close(self) -> None:
        await self._data.close()
