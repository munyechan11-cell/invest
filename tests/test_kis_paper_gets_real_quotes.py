"""모의투자 호스트는 **과거 일봉을 주지 않습니다.**

키 검증을 눌렀더니 이렇게 나왔습니다:

    HTTPStatusError: Server error '500 Internal Server Error' for url
    'https://openapivts.koreainvestment.com:29443/uapi/domestic-stock/v1/
     quotations/inquire-daily-itemchartprice?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=00

토큰도 받았고 현재가도 왔는데 일봉만 500 입니다. 키 문제가 아니라 **그
창구가 그 환경에 없는 것** 입니다. 그런데 화면은 잘린 URL 이 박힌 예외
이름을 보여 줬고, 그걸 읽고 할 수 있는 일은 없습니다 — 멀쩡한 키를 다시
발급받으러 가는 것 말고는.

봇은 워밍업에 수백 봉이 필요합니다. 그래서 **시세는 실계좌 키로 읽고 주문만
모의투자 계좌로** 냅니다. 같은 거래소, 같은 가격이고, 시세 제공자는 주문을
낼 수 있는 물건이 아닙니다.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from quant.cli import _load
from quant.live.credentials import VENUES_BY_ID
from quant.webapp.registry import _data_wiring, _flow_wiring, required_secrets

PAPER = ("configs/kr_kis_paper.yaml", "configs/us_kis_paper.yaml")
KIS_CONFIGS = [str(p) for p in sorted(Path("configs").glob("*.yaml"))
               if "provider: kis" in p.read_text(encoding="utf-8")]


# ── 설정이 어느 호스트를 보는가 ──────────────────────────────────────────
@pytest.mark.parametrize("path", KIS_CONFIGS)
def test_quotes_never_come_from_the_mock_host(path, monkeypatch):
    """모의투자 호스트는 일봉을 주지 않습니다. `paper: true` 로 두면 봇은
    워밍업에서 500 을 맞고, 그 500 은 "키가 틀렸다" 처럼 보입니다."""
    for var in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                "KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET",
                "KIS_PAPER_ACCOUNT_NO", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY"):
        monkeypatch.setenv(var, "x")
    config = _load(path)
    for label, section in (("data", config.data), ("flow", config.flow)):
        if section.provider != "kis":
            continue
        assert section.params.get("paper") is not True, (
            f"{path} 의 {label} 가 모의투자 호스트를 봅니다 — 그쪽에는 일봉이 "
            "없습니다")


@pytest.mark.parametrize("path", KIS_CONFIGS)
def test_quotes_use_the_real_keys(path, monkeypatch):
    """실계좌 호스트에 모의투자 키를 보내면 토큰부터 거절됩니다.
    반대도 마찬가지고, 그 두 실패는 같은 문장으로 돌아옵니다."""
    monkeypatch.setattr("quant.config.loader.os.environ", {}, raising=False)
    raw = Path(path).read_text(encoding="utf-8")
    for block in re.findall(r"^(data|flow):\n(?:[ #].*\n|\n)*", raw, re.M):
        if "provider: kis" not in block:
            continue
        assert "KIS_PAPER_APP_KEY" not in block, (
            f"{path}: 시세·수급이 모의투자 키를 씁니다")
        assert "${KIS_APP_KEY}" in block, f"{path}: 시세·수급에 실계좌 키가 없습니다"


def test_orders_still_go_to_the_paper_account():
    """시세를 실계좌에서 읽는다고 주문까지 실계좌로 가면, 이건 고친 게
    아니라 사고입니다."""
    for path in PAPER:
        raw = Path(path).read_text(encoding="utf-8")
        broker = raw.split("broker:", 1)[1]
        assert "KIS_PAPER_APP_KEY" in broker and "environment: paper" in broker
        assert "${KIS_APP_KEY}" not in broker.split("limits:")[0]


# ── 배선(웹앱 경로) ──────────────────────────────────────────────────────
@pytest.mark.parametrize("path", PAPER)
def test_the_web_wiring_agrees_with_the_yaml(path, monkeypatch):
    for var in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_PAPER_APP_KEY",
                "KIS_PAPER_APP_SECRET", "KIS_PAPER_ACCOUNT_NO",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY"):
        monkeypatch.setenv(var, "x")
    config = _load(path)
    assert _data_wiring(config).args["app_key"] == "KIS_APP_KEY"
    if config.flow.provider == "kis":
        assert _flow_wiring(config).args["app_key"] == "KIS_APP_KEY"
    needs = set(required_secrets(config))
    assert {"KIS_APP_KEY", "KIS_APP_SECRET"} <= needs, "시세용 실계좌 키"
    assert {"KIS_PAPER_APP_KEY", "KIS_PAPER_ACCOUNT_NO"} <= needs, "주문용 모의 키"
    assert "KIS_ACCOUNT_NO" not in needs, (
        "연습에 실계좌 **계좌번호** 까지 요구하면, 실계좌가 없는 사람은 "
        "연습조차 시작하지 못합니다")


def test_a_live_config_still_demands_the_real_account_number(monkeypatch):
    for var in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY"):
        monkeypatch.setenv(var, "x")
    needs = required_secrets(_load("configs/kr_desk_gemini.yaml"))
    assert "KIS_ACCOUNT_NO" in needs


def test_the_real_card_no_longer_forces_an_account_number():
    """시세만 쓰려는 사람이 계좌번호 칸에서 막히면 안 됩니다."""
    fields = {env: req for env, _label, req in VENUES_BY_ID["kis"].fields}
    assert fields["KIS_APP_KEY"] is True
    assert fields["KIS_ACCOUNT_NO"] is False


def test_the_real_card_says_why_a_practice_run_needs_it():
    note = VENUES_BY_ID["kis"].note_ko
    assert "시세는 이 키로" in note and "모의투자" in note


# ── 검증이 500 을 그대로 흘리지 않습니다 ────────────────────────────────
def verify(history_raises):
    from quant.api import server

    class _Provider:
        def __init__(self, **kw):
            pass

        async def quote(self, symbol):
            from datetime import datetime

            from quant.core.types import UTC, Quote
            return Quote(symbol, datetime.now(UTC), 71_200, 71_400)

        async def history(self, *a, **kw):
            if history_raises:
                raise RuntimeError(
                    "Server error '500 Internal Server Error' for url "
                    "'https://openapivts.koreainvestment.com:29443/uapi/"
                    "domestic-stock/v1/quotations/inquire-daily-itemchartprice'")
            from datetime import datetime, timedelta

            from quant.core.types import UTC, Bar
            t0 = datetime(2026, 9, 17, tzinfo=UTC)
            return [Bar(a[0], t0 - timedelta(days=i), 1, 1, 1, 1, 1, "1d")
                    for i in range(30)]

        async def close(self):
            pass

    import quant.data.providers.kis as kis_mod
    real_provider, real_token = kis_mod.KisProvider, kis_mod.kis_token

    async def _token(*a, **kw):
        return "t"

    kis_mod.KisProvider, kis_mod.kis_token = _Provider, _token
    try:
        return asyncio.run(server._verify_kis(
            {"KIS_PAPER_APP_KEY": "k", "KIS_PAPER_APP_SECRET": "s"}, paper=True))
    finally:
        kis_mod.KisProvider, kis_mod.kis_token = real_provider, real_token


def test_a_missing_daily_chart_is_not_reported_as_a_broken_key():
    out = verify(history_raises=True)
    assert out["ok"] is True, "키는 멀쩡한데 실패라고 하면 재발급하러 갑니다"
    assert "warning" in out and "과거 일봉을 주지 않습니다" in out["warning"]
    assert "실계좌" in out["warning"], "무엇을 해야 하는지가 없습니다"


def test_the_raw_http_error_does_not_reach_the_screen():
    out = verify(history_raises=True)
    assert "HTTPStatusError" not in str(out)
    steps = [s for s in out["steps"] if "일봉" in s["step"]]
    assert steps and steps[0]["ok"] is False, "일봉이 안 왔다는 사실은 남아야 합니다"


def test_a_healthy_paper_account_says_nothing_extra():
    out = verify(history_raises=False)
    assert out["ok"] is True and "warning" not in out


def test_the_screen_draws_the_warning():
    html = Path("quant/api/static/index.html").read_text(encoding="utf-8")
    assert "res.warning" in html, "경고를 서버가 보내도 화면이 안 그립니다"
    css = Path("quant/api/static/app.css").read_text(encoding="utf-8")
    assert ".vsum.warn" in css


# ── 기본값이 함정이 아닌가 ───────────────────────────────────────────────
def test_the_quote_providers_default_to_the_real_host():
    """기본값이 `paper=True` 였습니다. `paper` 를 안 적은 설정은 조용히
    시세 없는 문을 두드렸고, 돌아온 500 은 "키가 틀렸다" 처럼 보였습니다.

    환경변수 되돌림이 `KIS_APP_KEY`(실계좌 이름)를 읽고 있었다는 것이 이미
    같은 사실을 말하고 있었습니다 — 기본 호스트만 반대였습니다."""
    import inspect

    from quant.data.providers.kis import KisProvider
    from quant.data.providers.kis_flow import KisFlowProvider

    for cls in (KisProvider, KisFlowProvider):
        assert inspect.signature(cls).parameters["paper"].default is False, (
            f"{cls.__name__} 이 시세가 없는 호스트를 기본으로 봅니다")


def test_the_broker_still_defaults_to_paper_trading_off_not_the_host():
    """주문 쪽 기본값은 건드리지 않습니다 — 거기는 모의투자가 정상 동작하는
    창구이고, 환경은 설정이 명시적으로 고릅니다."""
    import inspect

    from quant.brokerage.kis_broker import KisBrokerage

    params = inspect.signature(KisBrokerage).parameters
    assert params["environment"].default == ""
    assert params["paper_trading"].default is False


# ── "연습하는데 왜 실계좌 키?" ───────────────────────────────────────────
def missing(*names):
    from quant.webapp.registry import CredentialsMissing

    return CredentialsMissing([
        {"name": n, "label": n, "venue": "kis", "venue_label": "한국투자증권 실계좌"}
        for n in names])


def test_the_refusal_says_why_practice_needs_the_real_keys():
    """이 문장이 없으면 사람은 자기가 설정을 잘못 골랐다고 생각하고,
    **실거래 설정으로 옮겨 갑니다.** 그게 이 안내가 필요한 이유입니다."""
    text = str(missing("KIS_APP_KEY", "KIS_APP_SECRET"))
    assert "모의투자 호스트가 과거 시세를 주지 않기 때문" in text
    assert "주문은 그대로 모의투자 계좌로" in text
    assert "계좌번호는 넣지 않으셔도" in text


def test_a_live_shortfall_does_not_get_the_practice_explanation():
    """계좌번호가 없다는 것은 실거래를 하려는 것입니다 — 거기에 "연습용
    전략인데" 를 붙이면 틀린 말이 됩니다."""
    text = str(missing("KIS_APP_KEY", "KIS_ACCOUNT_NO"))
    assert "연습용 전략" not in text


def test_the_explanation_survives_the_wire():
    payload = missing("KIS_APP_KEY").to_dict()
    assert "모의투자 호스트가 과거 시세를" in payload["error"]
    assert [i["name"] for i in payload["missing"]] == ["KIS_APP_KEY"]
