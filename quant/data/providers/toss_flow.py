"""토스 수급 프로바이더 — 투자자별 매매동향 (외국인·기관·개인·기타법인).

    GET /api/v1/stocks/{symbol}/investor-trading

**이 파일은 정정입니다.** 저장소 곳곳에 "수급 자료는 KIS 가 제공합니다 (토스는
미제공)" 이라고 적혀 있었고, 그건 틀렸습니다. 토스 공식 스펙에 국내 종목의
투자자별 매매동향이 있습니다. 그 오해 때문에 토스만 연동한 사람은 수급을 쓰려면
증권사를 하나 더 뚫어야 했습니다.

다만 KIS 와 **같은 자료는 아닙니다.** 세 가지가 다르고, 셋 다 조용히 틀린 숫자를
만들 수 있는 자리입니다.

**1. 금액 축이 없습니다.** 토스가 주는 것은 거래**량**(주, 정수)뿐입니다. 그래서
`InvestorFlow` 의 `*_value` 는 전부 0 으로 둡니다. 종가를 곱해 채우고 싶어지지만
그건 실제 체결 단가가 아니라 추정치이고, 그 위에서 계산되는 "수급 강도"(거래대금
대비 비중)가 화면에는 확신 있게 뜨면서 조용히 틀립니다. 없는 것은 없는 채로
둡니다 — 지속성·참여율·다이버전스는 수량만으로 전부 계산됩니다
(`FlowSummary.smart_money_side` 참고).
종목별 매매**대금**은 토스 API 어디에도 없습니다.
`GET /api/v1/market-indicators/{symbol}/investor-trading` 이 금액을 주긴 하지만
그건 KOSPI·KOSDAQ **시장 전체** 지표라(symbol 이 `KOSPI`/`KOSDAQ` 둘뿐입니다)
종목 수급에는 쓸 수 없습니다.

**2. `foreigner` 는 등록외국인만입니다.** 미등록 외국인이 빠져 있어서, 이름은
KIS 의 `frgn_ntby_qty` 와 같아도 **같은 집단이 아닙니다.** 한 종목의 시계열을 두
소스로 이어 붙이면 경계에서 계단이 생기고, 그 계단은 알파에게 매집·분산으로
보입니다. 한 종목은 한 소스로만 받으세요.

**3. 당일 기록은 잠정치입니다.** `_is_final()` 에 근거를 적어 두었습니다.

거래량·종가는 여기 없어서 일봉(`/api/v1/candles`)에서 채웁니다. 참여율 —
"오늘 거래량 중 외국인·기관 순매수가 얼마였나" — 만이 대형주와 소형주를 같은
임계값으로 비교할 수 있는 형태이기 때문입니다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from quant.brokerage.toss_broker import TossProvider
from quant.core.types import UTC, Symbol
from quant.data.flow import FlowProvider, InvestorFlow, register_flow_provider

log = logging.getLogger("quant.data.toss_flow")

_PATH = "/api/v1/stocks/{symbol}/investor-trading"

#: 스펙상 한 번에 받을 수 있는 최대치. 작게 부르면 같은 자료를 더 여러 번
#: 나눠 받을 뿐이고, 크게 부르면 400 `invalid-request` 입니다.
_PAGE = 100

#: 커서가 끝나지 않을 때를 대비한 상한. 100건씩 40페이지면 4,000 세션(≈16년)
#: 이라, 여기에 걸리는 것은 자료가 그만큼 있다는 뜻이 아니라 페이징이
#: 고장났다는 뜻입니다.
_MAX_PAGES = 40


def _num(raw) -> float:
    """스펙의 decimal 문자열 → 수. 값이 비어 있으면 0."""
    if raw in (None, ""):
        return 0.0
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return 0.0


def _net(block) -> float:
    """`InvestorTradingVolume` 의 순매수 주식 수. 매수 − 매도, 음수면 순매도."""
    if not isinstance(block, dict):
        return 0.0
    return _num(block.get("netBuyVolume"))


def _is_final(row: dict) -> bool:
    """이 기록이 확정치인가.

    `updatedAt` 만으로는 판정할 수 없습니다. 스펙에 따르면 한 일자의 기록은
    시간에 걸쳐 완성됩니다 — 투자자별 확정치는 당일 저녁, CFD 잔고는 다음
    영업일 새벽(T+1), 외국인 보유는 다음 영업일 오전 — 그리고 `updatedAt` 은
    그 **전부**를 포함한 마지막 갱신 시각입니다. 그래서 늦은 `updatedAt` 은
    "CFD 가 붙었다" 는 뜻일 수도 있어 투자자별 숫자가 확정됐다는 증거가 되지
    못하고, 시각 하나를 기준으로 자르면 그 경계는 우리가 지어낸 것입니다.

    대신 스펙이 직접 말해 주는 표식을 씁니다: 당일 잠정 기록에는 `individual`
    (개인)이 null 이고 "확정치가 반영되는 당일 저녁부터 값이 채워집니다".
    개인 순매수는 우리가 실제로 쓰는 값이기도 합니다 — `retail_contrarian` 의
    z-score, `InvestorFlow.is_accumulation` 이 전부 그것을 봅니다. 없는 것을
    0 으로 채우면 "개인이 안 샀다" 로 읽히고, 그건 잠정 기록에서 가장 흔한
    상태(장중)이므로 화면 전체가 조용히 뒤집힙니다.

    `updatedAt` 은 버리지 않고 로그에만 남깁니다 — 잠정 행이 얼마나 묵은
    것인지는 운영자가 알아야 합니다.
    """
    return row.get("individual") is not None


def _session_ts(row: dict) -> datetime | None:
    """세션 날짜 → UTC 자정. `kis_flow` 와 같은 규약입니다.

    `FlowFeed` 의 no-look-ahead 비교(`f.ts <= now`)가 이 규약 위에서 돕니다.
    """
    raw = row.get("date")
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


@register_flow_provider("toss")
class TossFlowProvider(FlowProvider):
    """토스 Open API 의 투자자별 매매동향. 국내(KR) 종목 전용입니다."""

    name = "toss_flow"

    def __init__(self, client_id: str = "", client_secret: str = "", **kwargs):
        # 인증은 브로커와 같은 OAuth2 client_credentials 입니다. 토큰 발급·캐시·
        # 레이트리밋·`{"result": ...}` 벗기기는 전부 `TossProvider` 가 쓰는
        # 클라이언트에 이미 있으므로 복제하지 않고 그대로 씁니다. 덤으로 일봉도
        # 같은 커넥션으로 받습니다 — 키가 없으면 여기서 바로 터지고,
        # `build_flow_feed` 가 그걸 잡아 `NullFlowProvider` 로 내려갑니다.
        self._data = TossProvider(client_id, client_secret, **kwargs)
        self._client = self._data.client

    # ── 투자자별 매매동향 ────────────────────────────────────────────────
    async def _records(self, symbol: Symbol, start: datetime, end: datetime
                       ) -> dict[datetime, dict]:
        """[start, end) 를 덮을 때까지 커서를 따라가며 모읍니다.

        페이징은 캔들(`before`/`nextBefore`)과 같은 모양입니다 — 최신순으로
        `count` 개를 받고, 다음 페이지는 응답의 `nextUntil` 을 `until` 로
        되돌려 보냅니다. 기간(from/to)으로는 부를 수 없습니다.
        """
        path = _PATH.format(symbol=symbol.ticker)
        # `until` 은 "이 날짜까지"(inclusive) 이고, 미래 날짜를 보냈을 때의
        # 동작은 스펙에 없습니다. 창의 끝이 오늘보다 뒤면 아예 보내지 않습니다 —
        # 미지정이 곧 "가장 최신부터" 입니다. 창의 끝이 과거면 보내는 편이
        # 훨씬 낫습니다: 안 보내면 오늘부터 그 시점까지를 전부 넘겨 받습니다.
        today = datetime.now(UTC).date()
        until = end.date().isoformat() if end.date() <= today else None

        out: dict[datetime, dict] = {}
        provisional = 0
        for _ in range(_MAX_PAGES):
            params: dict = {"count": _PAGE}
            if until:
                params["until"] = until
            data = await self._client.request("GET", path, params=params)
            rows = data.get("records") or []
            if not rows:
                break
            oldest: date | None = None
            for row in rows:
                ts = _session_ts(row)
                if ts is None:
                    continue
                oldest = ts.date() if oldest is None else min(oldest, ts.date())
                if not _is_final(row):
                    provisional += 1
                    log.debug("%s %s 는 아직 잠정치입니다 (updatedAt=%s) — 건너뜁니다",
                              symbol.ticker, row.get("date"), row.get("updatedAt"))
                    continue
                # 스펙상 `foreigner`·`institution` 은 필수입니다. 그래도 없는
                # 행을 0 으로 채워 넣지는 않습니다 — 0 은 "순매수가 없었다" 로
                # 읽히고, 그건 우리가 모르는 것과 다른 말입니다.
                if not isinstance(row.get("foreigner"), dict) or \
                        not isinstance(row.get("institution"), dict):
                    continue
                out[ts] = row

            next_until = data.get("nextUntil")
            if not next_until:
                break
            if oldest is not None and oldest < start.date():
                break
            if until is not None and str(next_until) >= until:
                # 커서가 뒤로 가지 않으면 같은 페이지를 영원히 받습니다.
                log.warning("토스 수급 커서가 진행하지 않습니다 (%s → %s)",
                            until, next_until)
                break
            until = str(next_until)
        else:
            log.warning("토스 수급 페이징이 %d 페이지에서 멈췄습니다 (%s)",
                        _MAX_PAGES, symbol.ticker)

        if provisional:
            log.info("%s: 잠정 기록 %d 건을 제외했습니다 — 확정치는 당일 저녁에 나옵니다",
                     symbol.ticker, provisional)
        return out

    # ── 거래량·종가 ──────────────────────────────────────────────────────
    async def _volumes(self, symbol: Symbol, start: datetime, end: datetime
                       ) -> dict[date, tuple[float, float]]:
        """세션별 (거래량, 종가).

        일봉 timestamp 는 장 시작 시각(`09:00+09:00`)이라 `.date()` 가 곧 세션
        날짜입니다. 매매동향의 `date` 와 같은 축이라 이 키로 맞물립니다.
        """
        try:
            bars = await self._data.history(symbol, "1d", start, end)
        except Exception as exc:            # noqa: BLE001 — 수급 자체는 살립니다
            log.warning("토스 일봉 조회 실패 %s: %s — 참여율 없이 진행합니다",
                        symbol.ticker, exc)
            return {}
        if not bars:
            # 200 인데 봉이 비어 온 경우입니다. 예외가 안 나므로 위 except 가
            # 잡지 못하고, 조용히 지나가면 아래에서 거래량 0 짜리 기록이
            # 만들어집니다 — 재현된 결함이 정확히 이것이었습니다.
            log.warning("토스 일봉이 비어 왔습니다 %s (%s~%s) — 참여율 없이 "
                        "방향만 남깁니다", symbol.ticker, start.date(), end.date())
            return {}
        return {bar.ts.date(): (bar.volume, bar.close) for bar in bars}

    # ── FlowProvider 인터페이스 ──────────────────────────────────────────
    async def flows(self, symbol: Symbol, start: datetime, end: datetime
                    ) -> list[InvestorFlow]:
        records = await self._records(symbol, start, end)
        if not records:
            # 일봉까지 부를 이유가 없습니다. 국내 종목이 아니면 매매동향은
            # 400 이고, 그때 호출부는 빈 목록을 받습니다.
            return []
        volumes = await self._volumes(symbol, start, end)
        have_candles = bool(volumes)

        out: list[InvestorFlow] = []
        for ts in sorted(records):
            if not (start <= ts < end):
                continue
            row = records[ts]
            found = volumes.get(ts.date())
            if found is None:
                # 일봉은 받아 왔는데 이 세션만 비어 있다면 아직 닫히지 않은
                # 당일 봉입니다(장 마감 후 저녁에 수급 확정치가 먼저 나옵니다).
                # 거래량이 0 이면 participation 이 0 으로 읽히는데, 그 0 은
                # "수급이 없었다" 와 구분되지 않습니다 — 틀린 0 을 내보내느니
                # 이 세션을 다음 갱신으로 미룹니다.
                # 일봉을 아예 못 받은 경우(엔드포인트 실패)는 반대입니다:
                # 방향만이라도 남기는 편이 시계열이 통째로 비는 것보다 낫습니다.
                if have_candles:
                    continue
                found = (0.0, 0.0)
            volume, close = found

            institution = row["institution"]
            # 기관 세부 7분류. 확정 기록에만 있고, 여기 담기는 값도 순매수
            # 주식 수입니다 — 이 레코드의 다른 수량과 같은 단위입니다.
            breakdown = institution.get("breakdown") or {}
            detail = {name: _net(block) for name, block in breakdown.items()
                      if isinstance(block, dict)}

            out.append(InvestorFlow(
                symbol=symbol,
                ts=ts,
                foreign_qty=_net(row["foreigner"]),
                institution_qty=_net(institution),
                retail_qty=_net(row["individual"]),
                # `*_value` 는 0 입니다 — 모듈 상단 참고. 토스는 종목별 매매대금을
                # 주지 않고, 종가를 곱한 추정치는 체결 단가가 아닙니다.
                close=close,
                volume=volume,
                institution_detail=detail,
            ))
        return out

    async def close(self) -> None:
        await self._data.close()
