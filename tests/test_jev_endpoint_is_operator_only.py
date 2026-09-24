"""Jev 주소는 운영자만 정합니다 — 코드에 기본값 없이, 계정으로는 바꿀 수 없게.

저장소가 공개라 운영자의 Jev 서버 주소를 코드에 둘 수 없습니다. 그리고 그
주소는 운영자의 `JEV_API_KEY` 가 실려 가는 곳입니다. 가입자가 정할 수 있으면
자기 서버 주소를 넣어 운영자 토큰을 받아 갑니다. 여기서 고정하는 것:

* 주소가 없으면 클라이언트를 **만들 때** `JEV_MCP_URL` 을 말하며 실패합니다 —
  봇 시작과 `/api/evaluate` 도 그 이유를 그대로 말합니다(키 탓으로 돌리지 않고).
* `https://` 만 받습니다. `http://` 는 이 컴퓨터 안의 서버뿐입니다. 오류
  문장에는 호스트까지만 적습니다.
* 계정에 `JEV_MCP_URL` 을 저장할 수 없고, 계정에 그 이름의 값이 **이미 있어도**
  데스크가 토큰을 보내는 곳은 운영자의 주소 그대로입니다.
* httpx 의 요청 로그에도 주소의 경로·쿼리가 남지 않습니다.

네트워크는 나가지 않습니다(가짜 Jev = `httpx.MockTransport`).
"""
from __future__ import annotations

import logging
import os

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from quant.alpha import llm_client
from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import BadEndpoint, LLMClient, LLMConfig, LLMError, MissingKey
from quant.api.server import ACCOUNT_KEYS, ACCOUNT_OPERATOR_FIELDS, create_app
from quant.config.loader import load_config
from quant.config.schema import StrategyConfig
from quant.live.credentials import (
    OPERATOR_FIELDS,
    CredentialStore,
    rejection_reason,
)
from quant.strategy.builder import _build_desk
from quant.webapp import accounts as accounts_module
from quant.webapp.accounts import Accounts
from quant.webapp.registry import UserRegistry, _with_credentials
from tests.conftest import DUMMY_JEV_MCP_URL
from tests.test_api_agents import PASSWORD, SECRET, template
from tests.test_desk import make_ctx, run_desk
from tests.test_jev import FakeJev, ask_technical, jev_client

#: 가입자가 넣어 보는 주소. `.invalid` 라 어디로도 해석되지 않습니다.
ATTACKER = "https://attacker.example.invalid/collect"
OPERATOR_TOKEN = "OPERATOR-JEV-TOKEN-SENTINEL"


def _desk_spec(cfg):
    return next(m for m in cfg.alpha if m.type in ("desk", "council"))


def _offline(client: LLMClient, seen: list) -> LLMClient:
    """이 클라이언트의 요청을 가짜 Jev 로 돌리고, 요청을 `seen` 에 적는다."""
    fake = FakeJev()

    def handler(request):
        seen.append(request)
        return fake(request)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# ── 주소가 없으면 만들 때 실패한다 ───────────────────────────────────────────
def test_there_is_no_address_in_the_code():
    assert not hasattr(llm_client, "JEV_DEFAULT_URL")
    assert llm_client.JEV_URL_ENV == "JEV_MCP_URL"


@pytest.mark.parametrize("unset", ["missing", "blank"])
def test_no_address_fails_at_construction_and_names_the_variable(monkeypatch, unset):
    if unset == "missing":
        monkeypatch.delenv("JEV_MCP_URL", raising=False)
    else:
        monkeypatch.setenv("JEV_MCP_URL", "   ")
    with pytest.raises(BadEndpoint) as err:
        LLMClient(LLMConfig(provider="jev", api_key="k"))
    text = str(err.value)
    assert "JEV_MCP_URL" in text and "주소가 없습니다" in text
    # 키 문제로 읽히면 사람은 멀쩡한 키를 보러 갑니다.
    assert isinstance(err.value, LLMError) and not isinstance(err.value, MissingKey)
    # 데스크도 같은 자리에서 멈춥니다 — 첫 심의가 아니라 시작할 때.
    with pytest.raises(BadEndpoint, match="JEV_MCP_URL"):
        TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)


def test_a_shipped_desk_config_does_not_assemble_without_the_address(monkeypatch):
    spec = _desk_spec(load_config("configs/kr_desk_gemini.yaml"))
    assert spec.params["llm"]["provider"] == "jev"
    assert "base_url" not in spec.params["llm"]          # 출하 설정은 주소를 적지 않는다
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.delenv("JEV_MCP_URL", raising=False)
    with pytest.raises(BadEndpoint, match="JEV_MCP_URL"):
        _build_desk(spec, None)
    monkeypatch.setenv("JEV_MCP_URL", DUMMY_JEV_MCP_URL)
    assert _build_desk(spec, None).client._jev_url() == DUMMY_JEV_MCP_URL


def test_other_providers_do_not_need_the_jev_address(monkeypatch):
    monkeypatch.delenv("JEV_MCP_URL", raising=False)
    LLMClient(LLMConfig(provider="google", api_key="k"))
    LLMClient(LLMConfig(provider="anthropic", api_key="k"))


def test_the_strategy_base_url_comes_before_the_environment():
    seen: list = []
    client = _offline(LLMClient(LLMConfig(
        provider="jev", api_key="k", base_url="https://jev-alt.example.invalid/mcp")), seen)
    ask_technical(client)
    assert {str(r.url) for r in seen} == {"https://jev-alt.example.invalid/mcp"}


# ── https 만 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("source", ["env", "base_url"])
@pytest.mark.parametrize("url", [
    "http://jev.example.invalid/s3cr3t-path/mcp?token=s3cr3t",   # 원격 평문
    "http://10.0.0.5:8080/s3cr3t/mcp",
    "ftp://jev.example.invalid/s3cr3t",
    "ws://jev.example.invalid/s3cr3t",
])
def test_a_cleartext_or_foreign_scheme_is_refused_before_any_request(
        monkeypatch, source, url):
    """토큰(`Authorization: Bearer …`)이 평문으로 나가기 전에 거절합니다."""
    if source == "env":
        monkeypatch.setenv("JEV_MCP_URL", url)
        config = LLMConfig(provider="jev", api_key="k")
    else:
        config = LLMConfig(provider="jev", api_key="k", base_url=url)
    with pytest.raises(BadEndpoint) as err:
        LLMClient(config)
    text = str(err.value)
    assert "https://" in text
    assert ("JEV_MCP_URL" if source == "env" else "llm.base_url") in text
    assert "s3cr3t" not in text                          # 경로·쿼리는 옮기지 않는다


@pytest.mark.parametrize("url", [
    "https://user:s3cr3t@jev.example.invalid/api/mcp",      # 사용자 정보
    "jev.example.invalid/s3cr3t/mcp",                       # 스킴 없음
    "https:///s3cr3t",                                      # 호스트 없음
    "https://jev.example.invalid:notaport/s3cr3t",          # 포트가 숫자가 아님
])
def test_a_malformed_address_is_refused_without_echoing_it(monkeypatch, url):
    monkeypatch.setenv("JEV_MCP_URL", url)
    with pytest.raises(BadEndpoint) as err:
        LLMClient(LLMConfig(provider="jev", api_key="k"))
    assert "JEV_MCP_URL" in str(err.value)
    assert "s3cr3t" not in str(err.value)


@pytest.mark.parametrize("url", [
    "http://localhost:8765/mcp",
    "http://127.0.0.1:8765/mcp",
    "http://[::1]:8765/mcp",
    DUMMY_JEV_MCP_URL,
])
def test_https_and_a_local_http_server_are_accepted(monkeypatch, url):
    monkeypatch.setenv("JEV_MCP_URL", url)
    seen: list = []
    ask_technical(_offline(LLMClient(LLMConfig(provider="jev", api_key="k")), seen))
    assert seen and all(str(r.url) == url for r in seen)


# ── 로그에는 호스트까지만 ─────────────────────────────────────────────────────
def test_the_http_request_log_keeps_only_the_host(monkeypatch, caplog):
    """httpx 는 요청마다 INFO 로 전체 URL 을 적습니다. 운영자가 경로·쿼리에 둔
    것이 로그 파일로 가면 안 됩니다."""
    monkeypatch.setenv("JEV_MCP_URL",
                       "https://jev.example.invalid/private-path/mcp?tenant=s3cr3t")
    with caplog.at_level(logging.INFO, logger="httpx"):
        ask_technical(jev_client(FakeJev()))
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert lines, "httpx 가 요청 로그를 남기지 않았습니다 — 검사가 아무것도 보지 않습니다"
    assert all("https://jev.example.invalid/…" in line for line in lines), lines
    assert not any("private-path" in line or "s3cr3t" in line for line in lines), lines


# ── 운영자 전용: 계정은 이 주소를 저장하지도, 바꾸지도 못한다 ──────────────────
def test_the_address_is_an_operator_field_but_never_an_account_field(tmp_path):
    label = next(label for env, label, _ in OPERATOR_FIELDS if env == "JEV_MCP_URL")
    assert "Jev 서버 주소" in label and "MCP 엔드포인트" in label and "운영자 전용" in label
    # 1인 운영자는 설정 화면(.env)에 넣을 수 있습니다.
    assert rejection_reason("JEV_MCP_URL") == ""
    store = CredentialStore(tmp_path / "env.test")
    assert store.update({"JEV_MCP_URL": DUMMY_JEV_MCP_URL}).written == ["JEV_MCP_URL"]
    # 계정에는 아닙니다.
    assert "JEV_MCP_URL" not in ACCOUNT_KEYS
    assert "JEV_MCP_URL" not in {env for env, _, _ in ACCOUNT_OPERATOR_FIELDS}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)
    root = tmp_path / "templates"
    root.mkdir()
    raw = template("jev-desk")
    raw["data"]["warmup_bars"] = 120
    raw["alpha"] = [{"type": "desk", "params": {"llm": {"provider": "jev"}}}]
    (root / "jevdesk.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True),
                                       encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    monkeypatch.setenv("JEV_API_KEY", OPERATOR_TOKEN)
    monkeypatch.delenv("QUANT_API_TOKEN", raising=False)
    app = create_app(StrategyConfig.model_validate(template("운영자")),
                     state_path=str(tmp_path / "state.db"))
    with TestClient(app, base_url="https://desk.example") as c:
        r = c.post("/api/auth/register", json={"email": "me@x.com", "password": PASSWORD})
        assert r.status_code == 201, r.text
        yield c


def test_an_account_cannot_store_the_address(client):
    body = client.post("/api/setup", json={"values": {
        "JEV_MCP_URL": ATTACKER, "TELEGRAM_CHAT_ID": "12345"}}).json()
    assert "JEV_MCP_URL" in body["rejected"]
    assert "JEV_MCP_URL" not in body["written"]
    assert body["written"] == ["TELEGRAM_CHAT_ID"]      # 나머지는 그대로 저장
    setup = client.get("/api/setup").json()
    assert "JEV_MCP_URL" not in (setup.get("configured") or {})
    assert "JEV_MCP_URL" not in {f["env"] for f in setup["operator_fields"]}
    assert os.environ["JEV_MCP_URL"] == DUMMY_JEV_MCP_URL  # 프로세스 환경도 그대로


def test_a_stored_per_user_address_does_not_move_the_operators_token(tmp_path, monkeypatch):
    """API 는 저장을 막습니다(위). 그래도 옛 행이나 다른 경로로 계정에 그 이름의
    값이 **이미 있다면** — 데스크가 운영자 토큰을 보내는 곳은 바뀌지 않아야 합니다.
    클라이언트는 설정의 `llm.base_url`(운영자 YAML)과 프로세스 환경만 읽고,
    `_with_credentials` 는 데스크 설정에 `api_key` 만 넣습니다."""
    monkeypatch.setenv("JEV_API_KEY", OPERATOR_TOKEN)
    accounts = Accounts(tmp_path / "acc.db", secret="x" * 40)
    reg = UserRegistry(accounts, root=tmp_path / "users")
    user = accounts.register("attacker@example.com", "correct-horse-9")
    accounts.put_secret(user.id, "JEV_MCP_URL", ATTACKER)
    accounts.put_secret(user.id, "llm.base_url", ATTACKER)   # 이름을 바꿔 봐도
    cfg = load_config("configs/kr_desk_gemini.yaml")

    wired = _with_credentials(reg.prepare(user.id, cfg), accounts.secrets_for(user.id))
    assert "base_url" not in _desk_spec(wired).params["llm"]

    desk, own = reg.desk_for(user.id, cfg)
    assert own is False                                   # 운영자 토큰으로 돈다
    assert desk.client._jev_url() == DUMMY_JEV_MCP_URL
    seen: list = []
    ask_technical(_offline(desk.client, seen))
    assert seen
    assert {r.url.host for r in seen} == {"jev.example.invalid"}
    assert all(r.headers["authorization"] == f"Bearer {OPERATOR_TOKEN}" for r in seen)
    assert "attacker" not in " ".join(str(r.url) for r in seen)
    assert os.environ["JEV_MCP_URL"] == DUMMY_JEV_MCP_URL


# ── 잘못 배포된 서버는 시작할 때 이유를 말한다 ────────────────────────────────
def test_starting_without_the_address_says_so_and_does_not_blame_the_key(
        client, monkeypatch):
    monkeypatch.delenv("JEV_MCP_URL", raising=False)
    r = client.post("/api/trader/start", json={"config_path": "jevdesk"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "JEV_MCP_URL" in detail and "운영자" in detail, detail
    assert "키가 없습니다" not in detail and "llm 값을 확인하세요" not in detail, detail

    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "jevdesk"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "JEV_MCP_URL" in detail and "운영자" in detail, detail
    assert "키가 없습니다" not in detail and "키를 쓸 수 없습니다" not in detail, detail


def test_a_cleartext_address_is_refused_at_start_with_the_host_only(client, monkeypatch):
    monkeypatch.setenv("JEV_MCP_URL", "http://jev.example.invalid/s3cr3t/mcp")
    r = client.post("/api/trader/start", json={"config_path": "jevdesk"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "https://" in detail and "JEV_MCP_URL" in detail, detail
    assert "s3cr3t" not in detail


def test_the_preflight_names_the_variable_not_an_address():
    """404 는 주소 문제입니다. 고칠 곳을 말하되 주소 자체는 화면에 옮기지 않습니다."""
    def handler(request):
        return httpx.Response(404, text="The page could not be found\n\nNOT_FOUND")

    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run_desk(desk, make_ctx()) == []
    reason = desk.status()["disabled_reason"]
    assert reason.startswith("Jev 주소를 찾을 수 없습니다"), reason
    assert "JEV_MCP_URL" in reason and "llm.base_url" in reason, reason
    assert "jev.example.invalid" not in reason and "/api/mcp" not in reason, reason
