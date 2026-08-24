"""통화 환산 — 외화 금액이 장부로 들어오는 단 하나의 문.

지금 `Portfolio` 의 현금은 숫자 하나이고(`quant/core/account.py:36`),
`Symbol.quote_currency` 는 실려 다니기만 하고 환산되지 않습니다. 그래서 원화
종목과 달러 종목을 한 유니버스에 넣어도 **에러가 나지 않습니다** — 7만(원)과
250(달러)이 같은 자릿수로 더해져 평가금액·수익률·하루 손실 한도가 전부 뜻
없는 숫자가 되고, 화면에는 그 숫자가 멀쩡히 뜹니다. 조용히 틀린 숫자가 빈칸
보다 훨씬 나쁘다는 이 저장소의 규칙이 가장 아프게 걸리는 자리입니다.
설계 문서(`docs/cross_market.md`)가 통화를 크로스마켓의 **1단계**로 잡은 이유가
그것입니다.

이 모듈이 지키는 것은 셋입니다.

**1. 환율은 그 시각의 것을 씁니다.** 지금 환율로 어제 체결을 환산하면 손익에
환차익이 섞여 들어가고, 전략이 번 것과 원달러가 움직인 것을 영영 구분할 수
없게 됩니다. 그래서 `to_base` 는 `when` 을 **필수**로 받습니다 — 기본값을
"지금" 으로 두면 호출부는 대개 그 기본값을 쓰게 되고, 규칙은 있으나 마나가
됩니다. 토스 `GET /api/v1/exchange-rate` 가 `dateTime` 을 받으므로 이건 흉내가
아니라 실제로 받아지는 값입니다.

**2. 못 받으면 멈춥니다.** 마지막으로 성공한 값으로 이어 쓰지 않습니다. 환율이
하루 묵으면 그 하루의 모든 외화 주문이 틀린 크기로 나가는데, 조용히 틀린
크기로 사는 것보다 그 종목을 안 사는 편이 낫습니다. 이 클래스에 "마지막으로
아는 환율" 필드가 **없다는 것 자체가** 그 보증입니다 — 캐시는 시각별로만
찾고, 실패는 캐시에 남기지 않으며, 다른 시각의 값으로 대신하지 않습니다.
그러니 이 파일에 `self._last_rate` 같은 것을 더하지 마세요.

**3. 환산은 여기 한 곳에서만.** 두 곳에서 환산하면 언젠가 한 곳만 고칩니다.

언제 이게 안 통하는가
---------------------
· 토스가 과거 `dateTime` 에 대해 "가장 최신 환율" 을 돌려주기 시작하면, 이
  계층은 아무 오류 없이 1번 규칙을 잃습니다. 응답의 유효 구간
  (`validFrom`~`validUntil`)이 요청한 시각을 덮지 않으면 로그를 남기는 이유가
  그것입니다 — 여기서 자체 임계값을 두고 거절하지는 않습니다. 주말·장 마감
  시간에는 구간이 안 맞는 것이 정상이고, 없는 기준을 지어내면 그때마다 멀쩡한
  환산이 막힙니다.
· 환율은 **평가**용 매매기준율입니다. 실제 환전에 붙는 스프레드는 여기서 비용
  으로 물리지 않으므로, 외화 종목의 실현손익은 그 폭만큼 낙관적입니다.
· 통화 쌍은 소스가 아는 것만 됩니다. 토스는 KRW↔USD 뿐입니다.
"""
from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from quant.core.types import UTC

log = logging.getLogger("quant.fx")


class FxUnavailable(RuntimeError):
    """환율을 받지 못했습니다.

    예외로 만드는 것이 요점입니다. 이 자리에서 1.0 이나 직전 값을 돌려주면
    호출부는 성공으로 읽고, 틀린 크기의 주문이 그대로 나갑니다.
    """


@dataclass(frozen=True)
class FxRate:
    """한 시각의 환율 한 개. **1 `source` = `rate` `target`** 입니다.

    `base`/`quote` 라는 말을 일부러 쓰지 않았습니다. 이 저장소에서 "quote
    currency" 는 종목이 매겨진 통화(환산의 **출발**)를 뜻하는데, 외환 관례와
    토스 API 에서 `quoteCurrency` 는 표시 통화(환산의 **도착**)를 뜻해 역할이
    정확히 뒤집힙니다. 같은 단어를 두 뜻으로 쓰면 언젠가 1,380배 틀린 숫자가
    나오고, 그건 화면에서 눈에 띄지도 않습니다.
    """

    source: str
    target: str
    rate: float
    #: 이 환율이 적용되는 시각 — 조회할 때 우리가 물어본 시각.
    asof: datetime
    #: 소스가 알려준 유효 구간. 없으면 None (소스가 안 주는 경우).
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    origin: str = ""

    def covers(self, when: datetime) -> bool:
        """소스가 준 유효 구간이 `when` 을 덮는가. 구간을 모르면 판단 보류(True)."""
        if self.valid_from is None or self.valid_until is None:
            return True
        return self.valid_from <= when < self.valid_until


class FxRateSource(ABC):
    """환율 하나를 답하는 것 외에 아무것도 하지 않는 인터페이스."""

    name = "base"

    @abstractmethod
    async def rate(self, source: str, target: str, when: datetime) -> FxRate:
        """`when` 시각의 **1 source = ? target**.

        모르면 예외를 던지세요. 0 이나 1.0 을 돌려주면 호출부는 그것을 환율로
        믿고, 외화 평가금액이 통째로 사라지거나(0) 원화와 달러가 1:1 로
        더해집니다(1.0) — 이 계층이 막으려던 바로 그 고장입니다.
        """

    async def close(self) -> None:
        return None


_FX_REGISTRY: dict[str, Callable[..., FxRateSource]] = {}


def register_fx_source(name: str):
    def deco(cls):
        _FX_REGISTRY[name.lower()] = cls
        return cls

    return deco


def create_fx_source(name: str, **kwargs) -> FxRateSource:
    key = (name or "").lower()
    # 등록은 모듈을 import 할 때 일어납니다. 여기서 불러 두지 않으면 이름은
    # 맞는데 "그런 소스 없음" 이 되고, 하필 실거래 설정에서만 그렇게 됩니다.
    if key == "toss" and key not in _FX_REGISTRY:
        from quant.data.providers import toss_fx as _tfx  # noqa: F401
    if key not in _FX_REGISTRY:
        raise KeyError(f"unknown fx source {name!r}; available: {sorted(_FX_REGISTRY)}")
    return _FX_REGISTRY[key](**kwargs)


def available_fx_sources() -> list[str]:
    return sorted(_FX_REGISTRY)


def _at_minute(when: datetime) -> datetime:
    """조회·캐시가 함께 쓰는 시각의 알갱이 — UTC 분.

    토스 환율은 1분마다 갱신되고 응답의 유효 구간도 보통 1분입니다. 같은 분
    안의 두 요청은 같은 값을 받으므로, 초까지 그대로 들고 다니면 캐시가 한
    번도 맞지 않아 봉 하나에 종목 수만큼 호출이 나갑니다. 반대로 이보다 굵게
    자르면(시간·일) 그건 "그 시각의 환율" 이 아니게 됩니다.

    캐시 키와 실제 요청에 **같은 값**을 씁니다. 하나만 자르면 09:30:59 로
    조회한 값이 09:30:00 의 캐시에 앉아, 캐시에 들어 있는 시각과 그 값이
    가리키는 시각이 어긋납니다.
    """
    if when.tzinfo is None:
        # tz 없는 시각은 이 엔진 전체에서 UTC 로 읽습니다(`Insight.__post_init__`).
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).replace(second=0, microsecond=0)


class Fx:
    """`amount` 를 기준통화로. 장부에 들어가는 모든 외화 금액이 지나는 문.

    소스를 주지 않으면 **기준통화 전용** 환산기입니다 — 기준통화 금액은 그대로
    통과시키고, 외화는 1.0 으로 얼버무리지 않고 거절합니다. 이 저장소는 아직
    통화가 섞인 설정을 시작 단계에서 막으므로(`StrategyConfig`) 그게 지금의
    정상 상태이고, 그래서 단일 통화 운용은 이 계층이 있든 없든 **똑같은 숫자**
    를 냅니다: 조회도, 곱셈도 일어나지 않습니다.
    """

    def __init__(self, base_currency: str, source: FxRateSource | None = None,
                 max_entries: int = 512):
        self.base_currency = (base_currency or "").strip().upper()
        if not self.base_currency:
            raise ValueError("Fx 는 기준통화가 있어야 합니다 (예: 'KRW')")
        self.source = source
        self._max = max(int(max_entries), 1)
        self._cache: dict[tuple[str, datetime], FxRate] = {}
        self._order: list[tuple[str, datetime]] = []
        self._locks: dict[tuple[str, datetime], asyncio.Lock] = {}

    # ── 환산 ─────────────────────────────────────────────────────────────
    def is_base(self, currency: str) -> bool:
        return (currency or "").strip().upper() == self.base_currency

    async def to_base(self, amount: float, quote_currency: str,
                      when: datetime) -> float:
        """`quote_currency` 로 매겨진 `amount` 를 `when` 시각 환율로 기준통화로.

        `quote_currency` 는 **종목이 매겨진 통화**(출발)입니다. 토스 API 의
        `quoteCurrency` 는 표시 통화(도착)라 뜻이 반대이니, 이 값을 그대로
        넘기지 마세요 — `_source_target` 이 그 뒤집기를 한 곳에서 합니다.
        """
        if self.is_base(quote_currency):
            # 기준통화면 환율이라는 것이 존재하지 않습니다. 1.0 을 곱하지도
            # 않습니다 — float 곱셈은 값을 바꿀 수 있고(0.1*1.0 은 같지만
            # 부호 있는 0 이나 inf 는 다릅니다), 무엇보다 "환산 안 함" 과
            # "환율이 1.0" 은 다른 사건입니다. 앞의 것만 조회 없이 통과합니다.
            return amount
        return amount * await self.rate_to_base(quote_currency, when)

    async def rate_to_base(self, quote_currency: str, when: datetime) -> float:
        """기준통화 1단위당이 아니라, `quote_currency` 1단위가 기준통화 얼마인가."""
        if self.is_base(quote_currency):
            return 1.0
        return (await self.quote(quote_currency, when)).rate

    async def quote(self, quote_currency: str, when: datetime) -> FxRate:
        """환율 한 개를 시각과 유효 구간까지 통째로. 못 받으면 `FxUnavailable`."""
        currency = (quote_currency or "").strip().upper()
        if not currency:
            raise FxUnavailable("환산할 통화가 비어 있습니다")
        if currency == self.base_currency:
            return FxRate(currency, self.base_currency, 1.0, _at_minute(when),
                          origin="identity")
        if self.source is None:
            # 여기서 1.0 을 돌려주는 것이 바로 이 계층이 없애려는 고장입니다.
            raise FxUnavailable(
                f"{currency} → {self.base_currency} 환율 소스가 없습니다. "
                f"환율 없이 외화 금액을 장부에 넣으면 1 {currency} = "
                f"1 {self.base_currency} 로 더해지고, 그 숫자는 화면에 아무 경고 "
                f"없이 뜹니다."
            )

        at = _at_minute(when)
        key = (currency, at)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        # 같은 봉에서 외화 종목이 여럿이면 같은 시각을 동시에 묻습니다. 잠금이
        # 없으면 종목 수만큼 같은 호출이 나가고, 레이트 리밋은 그때 걸립니다.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
            rate = await self._fetch(currency, at)
            self._cache[key] = rate
            self._order.append(key)
            while len(self._order) > self._max:
                self._cache.pop(self._order.pop(0), None)
        self._locks.pop(key, None)
        return rate

    # ── 조회 ─────────────────────────────────────────────────────────────
    async def _fetch(self, currency: str, at: datetime) -> FxRate:
        source, target = self._source_target(currency)
        try:
            rate = await self.source.rate(source, target, at)
        except FxUnavailable:
            raise
        except Exception as exc:      # noqa: BLE001 — 실패 이유는 여러 가지이고,
            # 전부 같은 결론입니다: 이 봉에서 이 통화는 환산하지 않습니다.
            raise FxUnavailable(
                f"{source}→{target} 환율을 {at.isoformat()} 시점으로 받지 못했습니다: "
                f"{exc}. 마지막으로 받은 환율로 대신하지 않습니다 — 하루 묵은 "
                f"환율은 그날의 모든 외화 주문을 틀린 크기로 내보냅니다."
            ) from exc

        if not isinstance(rate, FxRate) or not math.isfinite(rate.rate) or rate.rate <= 0:
            # 0 이면 외화 평가금액이 통째로 사라지고, 음수면 손익의 부호가
            # 뒤집힙니다. 둘 다 화면에는 숫자로 뜹니다.
            raise FxUnavailable(
                f"{source}→{target} 환율이 쓸 수 없는 값입니다: "
                f"{getattr(rate, 'rate', rate)!r}"
            )
        if not rate.covers(at):
            # 거절하지는 않습니다 — 주말·장 마감에는 구간이 안 맞는 것이
            # 정상입니다. 다만 이게 잦아지면 소스가 "요청한 시각" 이 아니라
            # "지금" 을 돌려주고 있다는 신호이고, 그러면 손익에 환차익이 조용히
            # 섞입니다. 그 신호를 눈에 보이게만 해 둡니다.
            log.warning(
                "%s→%s 환율의 유효 구간(%s~%s)이 요청한 시각 %s 를 덮지 않습니다",
                source, target, rate.valid_from, rate.valid_until, at.isoformat(),
            )
        return rate

    def _source_target(self, currency: str) -> tuple[str, str]:
        """환산 방향 → 환율 쌍. 뒤집기는 이 한 줄에서만 일어납니다.

        `currency` 금액을 기준통화로 바꾸려면 **1 currency = ? base** 가
        필요합니다. 소스의 인자 이름(source/target)이 그 순서 그대로입니다.
        """
        return currency, self.base_currency

    async def close(self) -> None:
        if self.source is not None:
            await self.source.close()
