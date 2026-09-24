"""데스크 실전 심의 1회 — 지연 시간·비용·품질 실측.

16석이 실제 LLM API로 한 바퀴 돌 때 몇 초 걸리고 얼마가 나오는지, 그리고 각
좌석이 정말 서로 다른 말을 하는지 확인하는 스크립트입니다. 실시간 자동매매에서는
심의가 한 봉 안에 끝나야 하므로, 이 숫자가 곧 사용 가능한 최소 봉 주기를 정합니다.

    python scripts/desk_live_check.py                 # 기본 (1종목, 토론 1라운드)
    python scripts/desk_live_check.py --rounds 2      # 더 깊게
    python scripts/desk_live_check.py --model claude-sonnet-5   # 더 싸게
    python scripts/desk_live_check.py --provider jev  # 출하 설정과 같은 Jev (JEV_API_KEY)

Jev 는 16석을 좌석당 호출 한 번으로 판단합니다(종목당 16회 + 시작 때 점검 1회).
글을 쓰지 않고 확률만 돌려주므로 서술 칸은 확률을 적은 정해진 문장이고,
`--max-tokens` 와 `--model` 은 쓰지 않습니다. **실제 호출이라 과금됩니다.**

주문은 나가지 않습니다 — `deliberate()` 만 호출하고 엔진 파이프라인은 타지 않습니다.
합성 데이터를 쓰므로 결과의 방향성 자체에는 의미가 없고, 측정 대상은 기계 쪽입니다.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant.live.credentials import load_env_file

load_env_file()

from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import DEFAULT_MODELS, LLMConfig
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, RunMode, Symbol
from quant.data.flow import FlowFeed
from quant.data.providers.local import SyntheticProvider
from quant.data.providers.synthetic_flow import SyntheticFlowProvider

BAR = "=" * 72


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="데스크 실전 심의 1회")
    p.add_argument("--ticker", default="005930")
    p.add_argument("--provider", default="auto",
                   choices=["auto", "jev", "anthropic", "google", "openai"],
                   help="auto = .env 에 있는 키를 보고 고름 (Jev 가 먼저)")
    p.add_argument("--model", default="", help="비우면 프로바이더 기본 모델")
    p.add_argument("--rounds", type=int, default=1, help="강세/약세 토론 라운드")
    p.add_argument("--risk-rounds", type=int, default=1)
    p.add_argument("--deadline", type=float, default=300.0)
    p.add_argument("--max-tokens", type=int, default=2400,
                   help="좌석 응답 한도. 낮으면 잘려서 재시도 비용이 붙습니다")
    p.add_argument("--rpm", type=float, default=0.0,
                   help="분당 요청 상한 (무료 티어는 5~15). 0 = 제한 없음")
    p.add_argument("--seats", default="",
                   help="분석 좌석 축소, 쉼표 구분 (예: technical,flow,quant)")
    p.add_argument("--json", default="", help="심의 결과를 이 경로에 JSON 으로 저장")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def pick_provider(requested: str) -> str:
    """Choose a provider from whatever credentials are actually present.

    Defaulting to one provider and failing when its key is missing sends the
    operator hunting for a config problem that is really a "you have a
    different key" problem.
    """
    # Jev 가 먼저인 이유: 출하 데스크 설정이 전부 Jev 라, 키가 여럿이면 실제로
    # 돌게 될 것을 재는 쪽이 맞습니다.
    available = [name for name, var in (
        ("jev", "JEV_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
    ) if os.environ.get(var, "").strip()]
    if requested != "auto":
        if requested not in available:
            print(f"{requested} 키가 .env 에 없습니다. "
                  f"사용 가능: {', '.join(available) or '없음'}", file=sys.stderr)
            return ""
        return requested
    if not available:
        print("LLM 키가 하나도 없습니다 — .env 에 JEV_API_KEY / ANTHROPIC_API_KEY / "
              "GOOGLE_API_KEY / OPENAI_API_KEY 중 하나를 넣으세요.", file=sys.stderr)
        return ""
    if len(available) > 1:
        print(f"사용 가능한 키: {', '.join(available)} → {available[0]} 사용 "
              f"(--provider 로 지정 가능)")
    return available[0]


async def run(args: argparse.Namespace) -> int:
    provider = pick_provider(args.provider)
    if not provider:
        return 2
    model = args.model or DEFAULT_MODELS.get(provider, "")
    if provider == "jev" and args.model and args.model != DEFAULT_MODELS["jev"]:
        # Jev 에는 고를 모델이 없습니다. 다른 이름을 적으면 단가표에서 못 찾아
        # 비용이 가장 비싼 기본 요율로 계산됩니다.
        print(f"Jev 에는 모델 선택이 없습니다 — --model {args.model} 은 무시합니다",
              file=sys.stderr)
        model = DEFAULT_MODELS["jev"]

    symbol = Symbol(args.ticker, venue="kis", quote_currency="KRW",
                    tick_size=Decimal("100"), lot_size=Decimal("1"))
    end = datetime.now(UTC)
    start = end - timedelta(days=420)

    bars = await SyntheticProvider(seed=11, start_price=72_000,
                                   annual_vol=0.28).history(symbol, "1d", start, end)
    if len(bars) < 260:
        print("합성 데이터가 부족합니다", file=sys.stderr)
        return 1

    portfolio = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(bars[-1].end_ts), portfolio, EventBus(),
                  timeframe="1d", run_mode=RunMode.DRY_RUN)
    ctx.universe = [symbol]
    ctx.seed_history(symbol, bars)
    portfolio.mark(symbol, bars[-1].close)

    feed = FlowFeed(SyntheticFlowProvider(seed=5, price=72_000,
                                          avg_volume=12_000_000), live=False)
    await feed.backfill([symbol], start, end)

    if provider == "jev":
        # 출하 설정(`llm: {provider: jev, timeout: 30}`)과 같게. 토큰 한도·온도는
        # Jev 에 뜻이 없어 넘기지 않습니다.
        llm = LLMConfig(provider="jev", model=model, timeout=30.0,
                        requests_per_minute=args.rpm,
                        max_retries=5 if args.rpm else 3)
    else:
        llm = LLMConfig(provider=provider, model=model, max_tokens=args.max_tokens,
                        temperature=0.2, requests_per_minute=args.rpm,
                        max_retries=5 if args.rpm else 3)
    desk = TradingDesk(
        llm,
        flow_feed=feed,
        debate_rounds=args.rounds,
        risk_debate_rounds=args.risk_rounds,
        max_symbols_per_run=1,
        deadline_s=args.deadline,
        allow_in_backtest=True,
        seats=[x.strip() for x in args.seats.split(",") if x.strip()] or None,
    )
    await desk.on_start(ctx)
    # 시작 점검(Jev 는 jev_check 한 번)은 종목 심의와 따로 적습니다 — 합쳐 적으면
    # "LLM 16회" 옆의 비용이 17회분이 됩니다.
    preflight_calls, preflight_cost = desk.status()["llm_calls"], desk.estimated_cost_usd
    if desk.status()["disabled_reason"]:
        print(f"\n{BAR}\n  데스크를 시작할 수 없습니다\n{BAR}")
        print(f"  {desk.status()['disabled_reason']}")
        print(BAR)
        return 2

    print(f"\n{BAR}\n  실전 심의 · {len(desk.seats)}석 · {symbol.ticker} · "
          f"종가 {bars[-1].close:,.0f}원 · {provider}/{model}\n{BAR}")

    decision = await desk.deliberate(ctx, symbol)
    if decision is None:
        print("심의가 완료되지 않았습니다 (마감시간 초과 또는 데이터 부족)")
        return 1

    usage = desk.client.usage
    print(f"\n{BAR}\n  {decision.summary_line()}\n{BAR}")
    # 소수 다섯째 자리까지 — Jev 는 종목당 $0.0005 안팎이라 `.3f` 로는 $0.000 입니다.
    print(f"  소요 {decision.elapsed_s:.1f}초 · LLM {decision.llm_calls}회 · "
          f"추정 ${decision.cost_usd:.5f} (종목 1개)")
    print(f"  시작 점검: LLM {preflight_calls}회 · 추정 ${preflight_cost:.5f}")
    print(f"  토큰 in {usage.input_tokens:,} / out {usage.output_tokens:,} (점검 포함)")
    failures = seat_failures(decision)
    if failures:
        print(f"  ⚠ 좌석 {len(failures)}곳 실패 — 첫 오류 [{failures[0][0]}] "
              f"{failures[0][1][:160]}")

    print("\n── 분석 8석 ──")
    for key_, report in decision.analysts.items():
        point = (report.get("key_points") or ["-"])[0]
        flag = "" if report.get("data_sufficient", True) else "[데이터부족] "
        print(f"  {key_:<15} {report.get('stance','?'):<8} "
              f"{report.get('conviction', 0):.2f}  {flag}{point[:74]}")

    print("\n── 강세 vs 약세 ──")
    for round_ in decision.debate.get("rounds", []):
        for side in ("bull", "bear"):
            arg = round_.get(side, {})
            print(f"  R{round_['round']} {side:<5}({arg.get('conviction', 0):.2f}) "
                  f"{arg.get('argument', '')[:140]}")
            if arg.get("concession"):
                print(f"        인정: {arg['concession'][:110]}")

    print("\n── 리스크 3석 ──")
    for round_ in decision.risk_debate.get("rounds", []):
        for side in ("aggressive", "conservative"):
            arg = round_.get(side, {})
            print(f"  {side:<13} 배율 {arg.get('proposed_scale', 0):.2f}  "
                  f"{arg.get('argument', '')[:110]}")
    print(f"  중립 판정     배율 {decision.position_scale:.2f} · veto={decision.vetoed}"
          f"  {decision.risk.get('reasoning', '')[:110]}")

    print("\n── 결정 3석 ──")
    print(f"  리서치매니저  [{decision.plan.get('rating','?')}] "
          f"{decision.plan.get('rationale','')[:120]}")
    print(f"  트레이더      {decision.trade.get('entry_style','?')} "
          f"{decision.trade.get('tranches','')}분할 · "
          f"{decision.trade.get('execution_note','')[:95]}")
    print(f"  데스크헤드    [{decision.action}] 확신 {decision.conviction:.2f} · "
          f"{decision.rationale[:130]}")

    if args.json:
        import json as _json
        Path(args.json).write_text(_json.dumps({
            "ticker": symbol.ticker, "close": bars[-1].close,
            "provider": provider, "model": model,
            "elapsed_s": decision.elapsed_s, "llm_calls": decision.llm_calls,
            "cost_usd": decision.cost_usd, "preflight_cost_usd": preflight_cost,
            "seat_failures": failures, "action": decision.action,
            "conviction": decision.conviction, "scale": decision.position_scale,
            "consensus": decision.consensus, "voting_seats": decision.voting_seats,
            "vetoed": decision.vetoed, "degraded": decision.degraded,
            "analysts": decision.analysts, "debate": decision.debate,
            "risk_debate": decision.risk_debate, "risk": decision.risk,
            "plan": decision.plan, "trade": decision.trade,
            "rationale": decision.rationale, "invalidation": decision.invalidation,
            "dissent": decision.dissent,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  → {args.json} 에 저장했습니다")

    print(f"\n  무효화 조건: {decision.invalidation}")
    print(f"  반대 의견:   {decision.dissent or '(없음)'}")
    print(f"  분석가 합의: {decision.consensus:+.2f} "
          f"(투표 {decision.voting_seats}/8석, 반대 {decision.dissent_ratio:.0%})")

    # The number that decides whether this is usable in real time.
    print(f"\n{BAR}")
    if failures:
        # 실패한 좌석은 기다리지 않고 대체값을 냅니다. 그 심의의 소요 시간은
        # 실제 심의보다 짧아서, 그걸로 봉 주기를 권하면 틀린 권고가 됩니다.
        print(f"  좌석 {len(failures)}곳이 답하지 못해 속도·비용 판정을 하지 않습니다.")
        for where, error in failures[:5]:
            print(f"    - {where}: {error[:140]}")
        print(BAR)
        return 1
    print(f"  실시간 적용 판정: 심의 {decision.elapsed_s:.0f}초 → "
          f"최소 봉 주기 {_min_timeframe(decision.elapsed_s)} 이상 권장")
    print(f"  10종목을 매 봉 심의하면 한 봉에 약 ${decision.cost_usd * 10:.5f} "
          f"(1시간봉이면 시간당, 1분봉이면 그 60배)")
    print(BAR)
    return 0


def seat_failures(decision) -> list[tuple[str, str]]:
    """답하지 못한 좌석과 그 오류. 좌석은 실패해도 대체값을 내므로 따로 셉니다."""
    found: list[tuple[str, str]] = []

    def check(where: str, report) -> None:
        if isinstance(report, dict) and report.get("error"):
            found.append((where, str(report["error"])))

    for key, report in decision.analysts.items():
        check(key, report)
    for round_ in decision.debate.get("rounds", []):
        for side in ("bull", "bear"):
            check(f"{side} R{round_.get('round')}", round_.get(side))
    for round_ in decision.risk_debate.get("rounds", []):
        for side in ("aggressive", "conservative"):
            check(f"risk_{side} R{round_.get('round')}", round_.get(side))
    check("risk_neutral", decision.risk)
    check("research_manager", decision.plan)
    check("trader", decision.trade)
    if decision.degraded:
        found.append(("head", decision.degraded))
    return found


def _min_timeframe(seconds: float) -> str:
    """심의가 한 봉 안에 끝나야 하므로 여유 2배를 두고 판정."""
    budget = seconds * 2
    for label, span in (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600),
                        ("4h", 14_400), ("1d", 86_400)):
        if budget <= span:
            return label
    return "1d"


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "quant.context", "quant.protections"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(run(args)))
