"""실거래 전에 산수로 미리 알 수 있는 것 — 돌려보기 전에 말해 줍니다.

여기 있는 검사는 전부 **경고만** 합니다. 설정을 거절하지 않고 값을 고치지도
않습니다. 막으려는 것은 잘못된 설정이 아니라 **조용한 설정** 입니다.

세 숫자가 서로 모르는 채로 정해집니다:

  · `portfolio.max_position_weight`   한 종목에 자산의 몇 %
  · `execution.min_order_notional`    이보다 작은 신규 주문은 건너뜀
  · `broker.max_order_notional`       이보다 큰 신규 주문은 거절

앞의 둘은 전략의 언어("자산의 30%")로, 뒤의 것은 계좌의 언어("한 건 $300")로
적혀 있습니다. 실거래 사이징은 보통 설정의 `starting_cash` 가 아니라
**증권사가 말하는 실제 평가액** 을 읽으므로, 그 둘이 어긋나면 계좌가 조금만
커져도 모든 진입이 거절되고 조금만 작아도 아무것도 사지 않습니다. 둘 다
예외가 아니라 침묵이라, 화면은 "대기 중" 만 보여 줍니다.

"보통" 인 이유: 증권사가 **장부의 통화로** 잔고를 말해 주지 못하면 봇은
그 숫자를 쓰지 않습니다(`_venue_cash` → `None`). 한투 잔고 창구는 원화
예수금만 주므로, 달러 장부로 한투를 쓰면 사이징 기준이 `starting_cash` 로
남습니다 — 그 경우 그 숫자가 **실제로 주문 크기를 정합니다.**
"""
from __future__ import annotations

from quant.config.schema import StrategyConfig
from quant.core.types import RunMode


def entry_window(config: StrategyConfig) -> tuple[float, float] | None:
    """신규 진입이 실제로 나가는 계좌 평가액 구간 `(하한, 상한)`.

    한 종목이 최대 비중까지 가는 경우입니다. 여러 종목이 동시에 잡히면
    `max_gross_leverage` 가 종목당 비중을 깎으므로 이 구간은 통째로 **위로**
    밀립니다 — 즉 아래 하한은 "이보다 작으면 확실히 못 산다" 입니다.

    `broker.max_order_notional` 은 **실제 어댑터에만** 걸립니다
    (`LiveBrokerage._guard`). 페이퍼는 그 값을 받지도 않으므로, 백테스트
    설정에 남아 있는 기본값을 상한으로 읽으면 있지도 않은 천장을 경고하게
    됩니다 — `configs/kr_equity.yaml` 이 실제로 그런 경우였습니다.
    """
    weight = config.portfolio.max_position_weight * (
        1.0 - config.portfolio.cash_reserve_pct)
    if weight <= 0:
        return None
    floor = config.execution.min_order_notional
    ceiling = config.broker.max_order_notional
    if floor <= 0 or ceiling <= 0 or config.broker.type == "paper":
        return None
    return floor / weight, ceiling / weight


def sizing_alarm(config: StrategyConfig, equity: float) -> str | None:
    """이 잔고에서 신규 진입이 한 건도 못 나가면 그 사실을, 아니면 None.

    `preflight_warnings` 는 설정만 보고 구간을 말합니다. 이건 봇이 증권사에
    연결한 **뒤** 에, 진짜 평가액을 손에 쥐고 다시 봅니다 — 그 둘이 다른
    질문이기 때문입니다. 설정은 어제 적혔고 잔고는 오늘 것입니다.

    말만 하고 아무것도 막지 않습니다. 막으면 이 검사 자체가 새로운 정지
    사유가 되고, 그건 고치려던 것보다 나쁩니다.
    """
    window = entry_window(config)
    if window is None or not equity or equity <= 0:
        return None
    low, high = window
    if low <= equity <= high:
        return None

    money = config.portfolio.base_currency or ""
    weight = config.portfolio.max_position_weight
    order = equity * weight * (1.0 - config.portfolio.cash_reserve_pct)
    head = (f"계좌 평가액 {equity:,.0f} {money} 에서 한 종목 최대 비중 "
            f"{weight:.0%} 는 주문 {order:,.0f} 입니다")
    # 청산·손절은 두 상한 어느 쪽에도 걸리지 않습니다. 그 사실을 같이 말하지
    # 않으면 이 문장이 "지금 포지션에 갇혔다" 로 읽힙니다.
    tail = "청산과 손절은 그대로 나갑니다"
    if equity < low:
        return (
            f"신규 진입이 나가지 않습니다 — {head}. 최소 주문금액 "
            f"{config.execution.min_order_notional:,.0f} 에 못 미쳐 건너뜁니다. "
            f"{tail}. 약 {low:,.0f} 이상이 필요하거나 "
            f"execution.min_order_notional 을 낮추세요"
        )
    return (
        f"신규 진입이 전부 거절됩니다 — {head}. 주문당 상한 "
        f"{config.broker.max_order_notional:,.0f} 를 넘습니다. {tail}. "
        f"broker.max_order_notional 을 올리거나 portfolio.max_position_weight "
        f"를 낮추세요"
    )


def _worth_saying(config: StrategyConfig, window: tuple[float, float]) -> bool:
    """이 구간을 사람에게 말할 가치가 있는가.

    백테스트 설정에서는 구간이 보통 몇백 배로 넓고, 그때 이 줄은 소음입니다 —
    늘 켜져 있는 경고는 아무도 안 읽습니다. 실거래는 다릅니다: 사이징이 읽는
    것이 설정의 가정이 아니라 진짜 잔고라, 그 숫자를 아는 것 자체가 시작 전에
    할 일입니다.
    """
    low, high = window
    if config.mode is RunMode.LIVE or low >= high:
        return True
    assumed = config.portfolio.starting_cash
    return not (low <= assumed <= high) or high < low * 3


def preflight_warnings(config: StrategyConfig) -> list[str]:
    """사람이 읽을 경고들. 비어 있으면 이 산수로는 걸릴 게 없다는 뜻입니다."""
    out: list[str] = []
    money = config.portfolio.base_currency or ""

    window = entry_window(config)
    if window is not None and _worth_saying(config, window):
        low, high = window
        if low >= high:
            out.append(
                f"신규 진입이 나갈 수 있는 계좌 평가액 구간이 없습니다 — "
                f"최소 주문({config.execution.min_order_notional:,.0f})이 이미 "
                f"주문당 상한({config.broker.max_order_notional:,.0f})보다 큰 "
                f"주문을 요구합니다. 어떤 잔고에서도 진입이 건너뛰거나 거절됩니다"
            )
        else:
            note = (
                f"신규 진입은 계좌 평가액이 약 {low:,.0f} ~ {high:,.0f} {money} "
                f"일 때만 나갑니다 (한 종목이 최대 비중 "
                f"{config.portfolio.max_position_weight:.0%} 로 갈 때). 이 아래면 "
                f"최소 주문금액에 못 미쳐 건너뛰고, 위면 주문당 상한에 걸려 "
                f"거절됩니다 — 둘 다 조용합니다"
            )
            if high < low * 2:
                note += ". 구간이 두 배도 안 돼서 잔고가 조금만 움직여도 벗어납니다"
            out.append(note)

    limits = config.limits
    floor = config.execution.min_order_notional
    if limits.max_daily_notional > 0 and limits.max_daily_orders > 0 and floor > 0:
        reachable = int(limits.max_daily_notional // floor)
        if reachable < limits.max_daily_orders:
            out.append(
                f"하루 주문 건수 한도 {limits.max_daily_orders}건은 걸리지 않습니다 — "
                f"거래대금 한도 {limits.max_daily_notional:,.0f} 를 최소 주문 "
                f"{floor:,.0f} 로 나누면 최대 {reachable}건입니다. 먼저 막는 것은 "
                f"언제나 거래대금 쪽입니다"
            )

    # ── 증권사 잔고를 못 읽는 조합 ───────────────────────────────────
    # 한투 잔고 창구(`inquire-balance`)가 주는 예수금은 **원화** 입니다.
    # 달러 장부에 그 숫자를 넣으면 1원 = 1달러 환산이 되므로 어댑터는
    # 아예 넘기지 않습니다(`kis_broker._venue_cash`). 막는 것까지는 맞는데,
    # 그 결과 사이징 기준이 조용히 `starting_cash` 로 남습니다 — 계좌를
    # 읽는 줄 알고 그 값을 대충 적어 둔 사람은 그걸 알 길이 없습니다.
    if config.broker.type == "kis" and money and money.upper() != "KRW":
        out.append(
            f"이 설정은 증권사 잔고를 사이징에 쓰지 못합니다 — 한투 잔고 창구는 "
            f"원화 예수금만 주는데 이 장부는 {money} 입니다. 그래서 "
            f"starting_cash({config.portfolio.starting_cash:,.0f} {money}) 가 "
            f"주문 크기를 정하는 실제 기준입니다. 계좌의 외화 예수금에 맞춰 "
            f"두세요 — 이 숫자가 실제보다 크면 진입이 거절되고, 작으면 "
            f"최소 주문금액에 못 미쳐 건너뜁니다. 둘 다 조용합니다"
        )

    if limits.max_daily_notional > 0 and config.mode is RunMode.LIVE:
        out.append(
            "청산 주문은 한도 검사는 면제받지만 거래대금 **사용량에는 기록**됩니다. "
            "한 번 사고 한 번 파는 왕복이 한도의 두 배를 씁니다"
        )
    return out
