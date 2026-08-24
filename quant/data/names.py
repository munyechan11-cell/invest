"""티커 → 사람이 읽을 수 있는 이름.

화면에 `005930` 만 떠 있으면 그 코드를 외운 사람만 쓸 수 있고, 잘못 고르면
**다른 회사를 삽니다**. 그래서 티커를 내보내는 자리마다 이름을 같이 싣습니다.

이름을 찾는 순서는 셋이고, 순서 자체가 근거의 세기입니다.

  (a) 상태 DB 의 `known_symbols` — 이 계정이 실제로 조회해 봐서 **증권사가
      직접 준** 이름. 사명이 바뀌거나 합병하면 여기가 먼저 맞아집니다.
  (b) 아래 정적 표 — 이 저장소가 기본으로 싣는 설정에 들어 있는 종목. 증권사
      키를 아직 등록하지 않은 사람, 장이 닫힌 시각, 증권사가 죽은 순간에도
      화면에 이름이 떠야 합니다. 그때 (a) 와 (c) 는 둘 다 비어 있습니다.
  (c) 프로바이더 조회 — 위 둘이 모르는 종목. 성공하면 (a) 에 적어 둡니다.

셋 다 실패하면 **티커를 그대로** 돌려줍니다. 지어낸 이름은 빈칸보다 훨씬
나쁩니다 — 사람은 화면에 뜬 이름을 읽고 주문 버튼을 누르기 때문입니다.

(c) 는 반드시 **묶어서** 물어봅니다. 종목 하나에 한 번씩 부르면 유니버스가
넓은 전략에서 화면 한 번 그리는 데 수십~수백 번이 나가고, 레이트 리밋에
걸리는 순간 이름이 **전부** 사라집니다 — 마침 이름이 가장 필요한 순간에.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("quant.data.names")

#: 이 표에 적힌 이름의 근거:
#:
#: * 국내 종목은 KRX 상장 정식 종목명입니다. 토스 공식 OpenAPI 문서의
#:   `GET /api/v1/stocks` 예시도 같은 값을 돌려줍니다(005930 → "삼성전자").
#: * 미국 종목은 국내 증권사 앱과 언론이 쓰는 통용 한글명입니다 — 토스 문서
#:   예시 역시 AAPL 을 "애플" 로 줍니다. 법인 영문명("APPLE INC")을 그대로
#:   쓰면 코드만 띄우는 것과 크게 다르지 않습니다.
#: * SPY 는 통용 한글명이 없습니다. 지어내는 대신 정식 상품명을 씁니다.
#: * 암호화폐는 거래소 심볼이 곧 이름이지만, 한글 통칭이 확립된 것만 답니다.
#:
#: 여기 없는 종목은 (c) 프로바이더 조회로 갑니다. 표를 손으로 늘리는 것보다
#: 조회가 정확합니다 — 손으로 적은 목록에는 반드시 틀린 줄이 섞이고, 틀린
#: 이름은 다른 회사를 사게 만듭니다. 이 표는 "기본 설정에 실린 종목"까지가
#: 경계이고, 그 이상은 증권사에게 물어봅니다.
STATIC_NAMES: dict[str, str] = {
    # ── 국내 주식 (configs/kr_equity.yaml, kr_toss*.yaml, kr_desk_gemini.yaml)
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "005380": "현대차",
    "051910": "LG화학",
    "207940": "삼성바이오로직스",
    # ── 미국 주식 (configs/us_equity.yaml, us_toss*.yaml)
    "AAPL": "애플",
    "MSFT": "마이크로소프트",
    "NVDA": "엔비디아",
    "GOOGL": "알파벳",
    "AMZN": "아마존",
    "META": "메타",
    "AVGO": "브로드컴",
    "LLY": "일라이릴리",
    "JPM": "JP모건체이스",
    "UNH": "유나이티드헬스",
    "WMT": "월마트",
    "XOM": "엑슨모빌",
    # 벤치마크로만 쓰이지만 화면에는 종목처럼 뜹니다.
    "SPY": "SPDR S&P 500 ETF",
    # ── 암호화폐 (configs/live_crypto.yaml)
    "BTC/USDT": "비트코인",
    "ETH/USDT": "이더리움",
    "SOL/USDT": "솔라나",
    "BNB/USDT": "바이낸스코인",
}

#: 한 요청에서 (c) 로 물어볼 종목 수의 상한. 토스가 한 번에 받는 최대치와 같은
#: 값입니다. 화면 하나가 이보다 많은 이름을 처음 보는 상황이라면, 남는 것은
#: 다음 요청에서 채워집니다 — 한 번에 다 채우려다 레이트 리밋에 걸리는 것보다
#: 낫습니다.
MAX_PROVIDER_ASK = 200

#: "물어봤는데 그런 종목을 모르더라" 를 잠깐만 기억합니다.
#:
#: 시세 화면은 몇 초마다 다시 그립니다. 끝내 이름이 없는 종목이 하나라도 있으면
#: 그때마다 증권사 호출이 새로 나가고, 그 호출은 레이트 리밋을 깎아서 정작
#: 필요한 시세 조회가 거절되게 만듭니다.
#:
#: 키에 상태 파일 경로를 함께 넣는 이유는 **사람마다 연동한 증권사가 다르기**
#: 때문입니다. A 의 증권사가 모르는 종목을 "아무도 못 찾는 것" 으로 치면, B 는
#: 자기 증권사에 물어볼 기회를 잃습니다.
#:
#: 영구 기록이 아니라 유예입니다. 신규 상장 종목은 몇 분 뒤 다시 물어봅니다.
_MISS_TTL_SECONDS = 900.0
_MISS_MAX = 4096
_MISSED: dict[tuple[str, str], float] = {}


def _missed_recently(scope: str, key: str) -> bool:
    until = _MISSED.get((scope, key))
    return until is not None and until > time.monotonic()


def _note_missed(scope: str, keys) -> None:
    now = time.monotonic()
    if len(_MISSED) > _MISS_MAX:
        for stale in [k for k, until in _MISSED.items() if until <= now]:
            _MISSED.pop(stale, None)
        if len(_MISSED) > _MISS_MAX:        # 전부 살아 있으면 통째로 버립니다
            _MISSED.clear()
    for key in keys:
        _MISSED[(scope, key)] = now + _MISS_TTL_SECONDS


def normalize(ticker: Any) -> str:
    """조회용 키. `"005930:toss"` 같은 심볼 키도 티커로 받아 냅니다.

    상태 DB 와 포지션은 `TICKER:VENUE` 형태의 심볼 키를 쓰는 자리가 있고,
    거기서 온 값을 그대로 찾으면 언제나 못 찾습니다.
    """
    text = str(ticker or "").strip()
    head = text.partition(":")[0].strip()
    return (head or text).upper()


def display(ticker: Any) -> str:
    """응답에 실을 티커 원문 — 대소문자를 바꾸지 않습니다."""
    return str(ticker or "").strip()


class NameBook:
    """요청 하나가 쓰는 이름 사전.

    요청 하나 동안만 캐시합니다. 프로세스 수명 내내 들고 있으면 사명 변경이
    재시작 전까지 반영되지 않고, 여러 사람이 도는 프로세스에서는 남의 상태
    파일에서 읽은 이름을 들고 다니게 됩니다.

    `store` 를 넘기면 이미 열려 있는 상태 DB 를 그대로 씁니다. 안 넘기면
    필요할 때 한 번 열고 닫습니다 — 이름을 하나도 안 찾는 요청은 DB 를 아예
    열지 않습니다.
    """

    def __init__(self, state_path: Any = "", store: Any = None):
        self._state_path = str(state_path or "")
        self._store = store
        self._seen: dict[str, str] | None = None
        #: 이번 요청에서 이미 프로바이더에 물어본 것. 못 찾은 종목을 같은
        #: 요청 안에서 두 번 묻지 않기 위한 것입니다.
        self._asked: set[str] = set()

    # ── (a) 조회 기록 ────────────────────────────────────────────────────
    def _remembered(self) -> dict[str, str]:
        if self._seen is not None:
            return self._seen
        found: dict[str, str] = {}
        store, opened = self._store, False
        try:
            if store is None and self._state_path:
                from quant.live.state import StateStore

                store, opened = StateStore(self._state_path), True
            if store is not None:
                for row in store.known_tickers():
                    name = str(row.get("name") or "").strip()
                    if name:
                        found[normalize(row.get("ticker"))] = name
        except Exception as exc:            # noqa: BLE001 — 이름 때문에 화면이
            # 죽으면 안 됩니다. 캐시를 못 읽으면 (b) 로 물러섭니다.
            log.debug("조회 기록에서 종목 이름을 읽지 못했습니다: %s", exc)
        finally:
            if opened and store is not None:
                store.close()
        self._seen = found
        return found

    # ── (a) + (b): 네트워크 없이 아는 것 ─────────────────────────────────
    def known(self, ticker: Any) -> str | None:
        """네트워크 없이 아는 이름. 모르면 None — 티커를 대신 넣지 않습니다."""
        key = normalize(ticker)
        if not key:
            return None
        return self._remembered().get(key) or STATIC_NAMES.get(key)

    def name(self, ticker: Any) -> str:
        """이름, 모르면 티커 그대로. 여기서 지어내는 값은 없습니다."""
        return self.known(ticker) or display(ticker)

    def label(self, ticker: Any) -> dict:
        """`{"ticker": ..., "name": ...}` — 티커 목록이 쓰는 모양."""
        return {"ticker": display(ticker), "name": self.name(ticker)}

    def labels(self, tickers) -> list[dict]:
        return [self.label(t) for t in tickers or []]

    def tag(self, rows, field: str = "ticker", into: str = "") -> list[dict]:
        """행마다 `<field>_name` 을 나란히 답니다.

        기존 필드는 그대로 둡니다 — 화면과 테스트가 이미 그것을 씁니다.
        """
        out_key = into or f"{field}_name"
        return [{**row,
                 # 티커 자리가 비어 있으면 이름도 비웁니다 (전량청산처럼 종목이
                 # 없는 줄). 빈 문자열을 넣으면 "이름이 안 나온 종목" 처럼
                 # 읽힙니다.
                 out_key: self.name(row[field]) if row.get(field) else None}
                for row in rows or [] if isinstance(row, dict)]

    # ── (c) 프로바이더 ───────────────────────────────────────────────────
    async def resolve(self, ticker: Any, provider: Any = None) -> str:
        """(a)→(b)→(c). 못 찾으면 티커 그대로."""
        found = await self.resolve_many([ticker], provider)
        return found.get(display(ticker)) or display(ticker)

    async def resolve_many(self, tickers, provider: Any = None) -> dict[str, str]:
        """여러 티커를 한 번에. 키는 넘긴 티커 원문, 값은 이름.

        (c) 로 갈 종목이 하나도 없으면 프로바이더를 아예 부르지 않습니다 —
        화면이 몇 초마다 다시 그리는 자리들이 이걸 부르기 때문입니다.
        """
        out: dict[str, str] = {}
        missing: list[str] = []
        for raw in tickers or []:
            shown = display(raw)
            out[shown] = self.name(raw)
            key = normalize(raw)
            if (key and not self.known(raw) and key not in self._asked
                    and not _missed_recently(self._state_path, key)):
                self._asked.add(key)
                missing.append(shown)
        if not missing or provider is None:
            return out

        asked = missing[:MAX_PROVIDER_ASK]
        found = await _ask_provider(provider, asked)
        learned: list[dict] = []
        for shown in asked:
            info = found.get(normalize(shown))
            name = str((info or {}).get("name") or "").strip()
            if not name:
                continue
            out[shown] = name
            self._remembered()[normalize(shown)] = name
            learned.append(info)
        self._remember(learned)
        # 못 찾은 것은 잠깐 쉬어 갑니다 — 화면이 다시 그릴 때마다 같은 질문을
        # 반복하면 그 자체가 레이트 리밋을 깎습니다.
        _note_missed(self._state_path,
                     [normalize(s) for s in asked if out[s] == s])
        return out

    def _remember(self, infos: list[dict]) -> None:
        """찾아낸 것을 (a) 에 적습니다 — 다음부터는 조회 없이, 이름으로도."""
        if not infos:
            return
        store, opened = self._store, False
        try:
            if store is None and self._state_path:
                from quant.live.state import StateStore

                store, opened = StateStore(self._state_path), True
            if store is None:
                return
            for info in infos:
                store.remember_ticker(info)
        except Exception as exc:            # noqa: BLE001 — 캐시를 못 적어도
            # 이번 응답의 이름은 이미 맞습니다. 다음 요청에서 다시 물어볼 뿐.
            log.debug("종목 이름을 조회 기록에 적지 못했습니다: %s", exc)
        finally:
            if opened and store is not None:
                store.close()


async def _ask_provider(provider: Any, tickers: list[str]) -> dict[str, dict]:
    """프로바이더에게 이름을 묻습니다 — 되도록 한 번에.

    `describe_many` 를 가진 프로바이더는 묶어서 묻고, 없으면 기본 구현이
    하나씩 돕니다. 실패는 조용히 삼킵니다: 이름이 없다고 시세 화면이 통째로
    죽으면, 고쳐 준 것보다 부순 것이 큽니다.
    """
    try:
        found = await provider.describe_many(tickers)
    except AttributeError:                  # describe_many 가 없는 옛 프로바이더
        found = None
    except Exception as exc:                # noqa: BLE001
        log.debug("종목 이름 조회 실패 (%d건): %s", len(tickers), exc)
        return {}
    if found:
        return {normalize(k): v for k, v in found.items() if isinstance(v, dict)}
    return {}
