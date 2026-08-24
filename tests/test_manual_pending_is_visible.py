"""접수한 수동 주문이 화면에 뜨고, 나가기 전에 취소되는가.

수동 주문은 눌리는 즉시 브로커로 가지 않습니다. 대기열에 들어갔다가 엔진이
다음 봉을 처리할 때 나갑니다. 그 사이가 이 파일이 다루는 구간입니다.

지금까지 그 구간은 화면에 **존재하지 않았습니다**. `ManualControl.cancel()`
도, `status()["pending"]` 도 이미 있었지만 그것을 부르는 라우트가 없었고
대시보드는 `pending` 을 한 번도 그리지 않았습니다. 사람 쪽에서 보면 매수를
누른 뒤 화면이 아무 말도 하지 않습니다. 그러면 다시 누릅니다 — 그래서 같은
주문이 두 번 나갑니다. 그리고 마음이 바뀌어도 무를 방법이 없습니다.

그래서 여기서 고정하는 성질은 셋입니다.

1. 접수된 주문은 목록에 **보인다** — 그리고 그 줄의 숫자는 접수된 값 그대로다.
   반올림된 지정가는 다른 주문입니다.
2. 취소는 **실제로** 대기열에서 뺀다 — 주문이 만들어지지 않는다.
3. 취소 버튼을 두 번 눌러도 요청은 한 번만 나간다.

3번이 특히 중요합니다. 취소 한 번에 왕복이 둘(POST + 새로고침)이라, 그 사이
버튼이 살아 있으면 사람은 한 번 더 누릅니다 — 화면이 안 바뀌면 다시 누른다는
것이 이 목록을 만든 이유 자체입니다. 두 번째 요청은 404 로 돌아오고, 그것이
늦게 도착해 초록 성공 문구를 빨간 "없는 주문" 으로 덮습니다. 취소는 성공했는데
화면은 발주됐다고 말하는 셈이고, 그다음 사람이 하는 일은 반대매매입니다.

화면 쪽 검사는 `index.html` 의 `PENDING BLOCK` 을 잘라 **자바스크립트 엔진에서
실제로 실행**합니다. 문자열이 들어 있는지 세는 검사로는 3번 같은 것을 잡을 수
없습니다 — 토큰은 다 있는데 순서만 틀린 코드가 정확히 그 사고를 냅니다.
엔진이 없는 환경에서는 건너뜁니다. 대신 라우트 쪽 검사는 어디서나 돕니다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.core.types import Symbol
from quant.live.manual import ManualControl
from quant.webapp import accounts as accounts_module
from quant.webapp.auth_api import SESSION_COOKIE

#: 아래 라우트 검사가 `tmp_path` 로 chdir 하므로 상대 경로로 두면 안 됩니다.
PAGE = Path(__file__).resolve().parent.parent / "quant" / "api" / "static" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


# ── (1) 대기열에서 빼면 주문이 만들어지지 않는다 ─────────────────────────
def test_a_cancelled_request_never_becomes_an_order():
    """취소의 뜻은 "목록에서 사라진다" 가 아니라 "안 나간다" 입니다."""
    from tests.test_manual_and_limits import ctx_with

    manual = ManualControl()
    manual.buy(Symbol("AAA", venue="SIM"), quantity=Decimal("1"))     # 남길 것
    drop = manual.buy(Symbol("BBB", venue="SIM"), quantity=Decimal("1"))

    assert manual.cancel(drop.id) is True
    assert manual.cancel(drop.id) is False, "두 번 취소되면 그건 취소가 아닙니다"

    orders = manual.build_orders(ctx_with())
    assert [o.symbol.ticker for o in orders] == ["AAA"]
    assert drop.status == "cancelled"


# ── (2) 라우트 ──────────────────────────────────────────────────────────
SECRET = "manual-pending-secret-0123456789abcdef"
PASSWORD = "korea-invest-1"

PAPER = {
    "name": "모의전략",
    "mode": "dry_run",
    "data": {"provider": "synthetic", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60},
    "universe": {"symbols": [{"ticker": "SIM1"}]},
    "alpha": [{"type": "ema_cross"}],
    "broker": {"type": "paper"},
}


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """PBKDF2 600,000회는 이 파일에서 재고 싶은 것이 아닙니다."""
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000)


@pytest.fixture
def client(tmp_path, monkeypatch):
    configs = tmp_path / "templates"
    configs.mkdir()
    (configs / "paper.yaml").write_text(
        yaml.safe_dump(PAPER, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(configs))
    monkeypatch.chdir(tmp_path)
    app = create_app(None, state_path=str(tmp_path / "state.db"))
    # 컨텍스트 매니저여야 lifespan 이 돌고 봇이 같은 이벤트 루프에 붙습니다.
    with TestClient(app, base_url="https://desk.example") as c:
        r = c.post("/api/auth/register",
                   json={"email": "a@example.com", "password": PASSWORD,
                         "display_name": "에이"})
        assert r.status_code == 201, r.text
        # 가입 응답이 세션 쿠키를 항아리에 넣어 줍니다. 여기서 또 넣으면 한
        # 요청에 두 장이 가고, 서버는 그걸 세션으로 보지 않습니다.
        yield c


@pytest.fixture
def running(client):
    """모의 브로커 + 합성 시세로 돌아가는 봇. 실제 증권사는 어디서도 안 부릅니다."""
    assert client.post("/api/trader/start",
                       json={"config_path": "paper"}).status_code == 200
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        status = client.get("/api/status").json()
        if status.get("running"):
            break
        if status.get("error"):
            raise AssertionError(f"봇이 죽었습니다: {status}")
        time.sleep(0.05)
    else:
        raise AssertionError("봇이 뜨지 않았습니다")
    try:
        yield client
    finally:
        client.post("/api/trader/stop")


def test_a_queued_order_is_visible_and_cancellable(running):
    """접수 → 목록에 보임 → 취소 → 목록에서 사라짐.

    화면이 부르는 주소가 실제로 존재하는지까지 봅니다. 대기 목록만 그리고
    취소 라우트가 없으면 "보이는데 못 무는" 상태가 되고, 그건 고치기 전과
    같은 결말(반대매매)로 갑니다.
    """
    queued = running.post("/api/manual/buy",
                          json={"ticker": "SIM1", "quantity": 1,
                                "limit_price": 0.0421})
    assert queued.status_code == 200, queued.text
    request_id = queued.json()["queued"]["id"]

    pending = running.get("/api/manual").json()["pending"]
    assert [p["id"] for p in pending] == [request_id], "접수한 주문이 목록에 없습니다"
    # 화면이 그릴 값이 응답에 그대로 실려 있어야 합니다 — 반올림해서 내려주면
    # 화면이 무엇을 하든 이미 다른 주문입니다.
    assert pending[0]["limit_price"] == 0.0421
    assert pending[0]["quantity"] == 1.0

    gone = running.post(f"/api/manual/cancel/{request_id}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["cancelled"] == request_id
    assert running.get("/api/manual").json()["pending"] == []


def test_cancelling_something_that_already_left_is_not_a_success(running):
    """없는 주문에 200 을 주면 "취소됐다" 로 읽힙니다 — 그건 거짓말입니다."""
    twice = running.post("/api/manual/cancel/man_does_not_exist")
    assert twice.status_code == 404
    detail = twice.json()["detail"]
    # 어느 쪽인지 서버는 모릅니다(다른 창에서 이미 취소했을 수도 있습니다).
    # 모르는 것을 아는 척하면 사람은 발주된 줄 알고 반대매매를 냅니다.
    assert "발주" in detail and "취소" in detail


def test_the_cancel_route_belongs_to_its_owner(running):
    """남의 대기 주문에 손댈 수 있으면 그건 취소가 아니라 사고입니다."""
    queued = running.post("/api/manual/buy", json={"ticker": "SIM1", "quantity": 1})
    request_id = queued.json()["queued"]["id"]

    mine = running.cookies.get(SESSION_COOKIE)
    running.cookies.clear()
    r = running.post("/api/auth/register",
                     json={"email": "b@example.com", "password": PASSWORD,
                           "display_name": "비"})
    assert r.status_code == 201
    # B 에게는 봇이 없습니다 — 그러니 A 의 대기열도 없습니다.
    assert running.post(f"/api/manual/cancel/{request_id}").status_code == 404

    running.cookies.clear()
    running.cookies.set(SESSION_COOKIE, mine)
    assert [p["id"] for p in running.get("/api/manual").json()["pending"]] == [request_id]


# ── (3) 화면 ────────────────────────────────────────────────────────────
def test_the_panel_has_a_place_to_draw_the_queue():
    """그릴 자리가 없으면 그리는 코드가 있어도 아무 데도 안 나옵니다.

    여기만 정적 검사입니다 — DOM 이 없는 곳에서 `refreshManual()` 을 통째로
    돌릴 수 없기 때문입니다. 그래서 이 세 줄이 "잘못된 내용을 그린다" 는
    잡지 못합니다. 그건 아래 실행 검사가 봅니다. 여기서 막는 것은 하나뿐:
    **연결이 통째로 사라지는 것.**
    """
    assert 'id="mPending"' in HTML
    assert re.search(r'\$\("#mPending"\)\.innerHTML\s*=\s*renderPending\(', SCRIPT), \
        "새로고침이 대기 목록을 그리지 않습니다"
    assert re.search(r'\$\("#mPending"\)\.querySelectorAll\("\[data-cancel\]"\)'
                     r'[\s\S]{0,120}cancelPending\(', SCRIPT), \
        "취소 버튼에 아무것도 연결되지 않았습니다 — 보이는데 못 무는 상태입니다"


#: 화면 코드에서 실제로 실행해 볼 조각. 마커 사이만 잘라내므로, 옮기면 여기가
#: 먼저 깨집니다 — 조용히 검사 대상 밖으로 빠져나가지 못하게.
def _pending_block() -> str:
    block = re.search(r"PENDING BLOCK START \*/(.*?)/\* PENDING BLOCK END",
                      SCRIPT, re.S)
    assert block, "index.html 에서 PENDING BLOCK 을 찾지 못했습니다"
    return block.group(1)


def _borrow(pattern: str) -> str:
    """화면이 쓰는 진짜 헬퍼를 그대로 빌려 옵니다.

    스텁으로 흉내 내면 `esc()` 를 통째로 빼먹은 코드도 통과합니다 — 실제로
    앞선 판이 그랬습니다.
    """
    found = re.search(pattern, SCRIPT, re.S | re.M)
    assert found, f"화면에서 {pattern!r} 를 찾지 못했습니다"
    return found.group(0)


_ENGINE = next(
    (p for p in (
        shutil.which("node"),
        "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc",
    ) if p and Path(p).exists()), None)

#: 바깥 세계. 진짜 `post`/`note`/`refreshManual` 대신 무엇이 몇 번 불렸는지만
#: 적어 두는 가짜를 넣고, 결과를 JSON 한 줄로 뱉습니다.
HARNESS = """
%(esc)s
%(when)s
const POSTS = [];
const INFLIGHT = [];
function post(path) {
  POSTS.push(path);
  return new Promise((res, rej) => { INFLIGHT.push({res: res, rej: rej}); });
}
/* 나가 있는 요청을 **전부** 끝냅니다. 하나만 끝내면, 가드가 빠져서 요청이 두
   번 나간 코드는 영영 안 끝나고 테스트는 타임아웃이나 JSON 오류로 죽습니다 —
   무엇이 틀렸는지 못 알려주는 실패는 반쯤 없는 테스트입니다. */
function settleAll(value) { while (INFLIGHT.length) INFLIGHT.shift().res(value); }
function failAll(message) {
  while (INFLIGHT.length) INFLIGHT.shift().rej(new Error(message));
}
let MSG = null;
function note(el, text, kind) { MSG = {el: el, text: text, kind: kind}; }
let REFRESHED = 0;
async function refreshManual() { REFRESHED += 1; }
function button(id) {
  return {dataset: {cancel: id}, disabled: false, textContent: "취소"};
}
%(block)s
async function main() {
%(body)s
}
main().then(out => print(JSON.stringify(out)),
            err => { print("HARNESS ERROR: " + err); });
"""


def run_js(body: str) -> dict:
    """`body` 를 화면 코드와 함께 돌리고, 그것이 돌려준 값을 파이썬으로."""
    source = HARNESS % {
        "esc": _borrow(r"^const esc = .*?\[c\]\)\);$"),
        "when": _borrow(r"^function when\(iso\) \{.*?^\}$"),
        "block": _pending_block(),
        "body": body,
    }
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(source)
        path = fh.name
    try:
        env = {**os.environ, "TZ": "UTC"}     # 보는 사람의 시계와 무관해야 합니다
        proc = subprocess.run([_ENGINE, path], capture_output=True, text=True,
                              timeout=60, env=env)
        assert proc.returncode == 0, (proc.stderr or proc.stdout)[:2000]
        out = proc.stdout.strip()
        assert not out.startswith("HARNESS ERROR"), out[:2000]
        assert out, ("화면 코드가 끝나지 않았습니다 — 아직 응답을 기다리는 요청이 "
                     "남아 있다는 뜻입니다 (기대보다 많이 나갔을 때 이렇게 됩니다)")
        return json.loads(out)
    finally:
        Path(path).unlink(missing_ok=True)


js = pytest.mark.skipif(_ENGINE is None, reason="자바스크립트 엔진이 없습니다")

ROW = """
  const row = {
    id: "man_abc", action: "buy", symbol: "BTC/USDT", symbol_name: "비트코인",
    quantity: 0.00004321, notional: null, limit_price: 0.0421,
    requested_at: "2024-08-21T06:40:00+00:00",
  };
"""


@js
def test_the_row_shows_the_numbers_that_were_submitted():
    """반올림된 지정가는 확인이 아니라 오해입니다.

    0.0421 로 낸 주문이 화면에 0.04 로 뜨면, 사람은 자기가 안 낸 주문을
    확인한 것이 됩니다. 소수점 아래가 살아 있는 종목에서 이건 흔한 일입니다.
    """
    # 화면이 실제로 부르는 것은 `renderPending` 입니다. 줄 한 개를 따로
    # 확인하면, 목록 전체가 통째로 빈 문자열을 돌려줘도 통과합니다.
    out = run_js(ROW + """
  return {text: pendingText(row), html: renderPending([row])};
""")
    assert "0.0421" in out["text"], out["text"]
    assert "0.00004321" in out["text"], out["text"]
    assert "비트코인 (BTC/USDT)" in out["text"]
    assert "매수" in out["text"]
    # 종목·방향·수량·지정가와 취소 버튼이 목록에 실제로 실려 있어야 합니다.
    for piece in ("매수", "비트코인", "0.00004321", "0.0421",
                  'data-cancel="man_abc"'):
        assert piece in out["html"], f"{piece} 가 목록에 없습니다: {out['html']}"


@js
def test_a_market_order_says_so_and_a_close_invents_no_quantity():
    """모르는 값은 안 적습니다 — 청산 수량은 발주 시점에야 정해집니다."""
    out = run_js("""
  return {
    market: pendingText({id: "m1", action: "buy", symbol: "SIM1", quantity: 3}),
    close: pendingText({id: "m2", action: "close", symbol: "SIM1"}),
    all: pendingText({id: "m3", action: "close_all"}),
    empty: renderPending([]),
  };
""")
    assert "시장가" in out["market"] and "지정가" not in out["market"]
    assert out["close"].startswith("청산") and "주" not in out["close"]
    assert out["all"] == "전체 청산"
    assert out["empty"] == "", "대기가 없으면 빈 상자도 그리지 않습니다"


@js
def test_a_ticker_cannot_smuggle_markup_into_the_row():
    out = run_js("""
  return {html: renderPending([{id: "m1", action: "buy",
                                symbol: "<img src=x onerror=alert(1)>", quantity: 1}])};
""")
    assert "<img" not in out["html"], out["html"]
    assert "&lt;img" in out["html"]


@js
def test_the_timestamp_does_not_depend_on_the_viewers_clock():
    """`+00:00` 로 온 시각이 보는 사람의 TZ 에 따라 달라지면 안 됩니다."""
    out = run_js(ROW + """
  return {at: when(row.requested_at)};
""")
    assert "08" in out["at"] and "21" in out["at"], out["at"]
    # 서울 기준 15:40. 실행 환경 TZ 는 UTC 로 고정해 두었습니다.
    assert "15:40" in out["at"] or "3:40" in out["at"], out["at"]


@js
def test_clicking_cancel_twice_sends_one_request():
    """여기가 지난번에 무너진 자리입니다.

    두 번째 요청은 404 로 돌아오고, 그 응답이 초록 성공 문구를 빨간 "없는
    주문" 으로 덮습니다 — 취소는 됐는데 화면은 발주됐다고 말합니다.
    """
    out = run_js("""
  const btn = button("man_abc");
  const first = cancelPending(btn);
  const second = cancelPending(btn);       // 왕복이 끝나기 전의 두 번째 클릭
  const duringFlight = pendingRow({id: "man_abc", action: "buy",
                                   symbol: "SIM1", quantity: 1});
  settleAll({cancelled: "man_abc"});
  await first; await second;
  return {posts: POSTS.length, msg: MSG, refreshed: REFRESHED,
          duringFlight: duringFlight, disabled: btn.disabled};
""")
    assert out["posts"] == 1, f"요청이 {out['posts']}번 나갔습니다"
    assert out["msg"]["kind"] == "ok", out["msg"]
    assert "취소 실패" not in out["msg"]["text"], out["msg"]
    assert out["refreshed"] == 1
    assert out["disabled"] is True
    # 8초 주기 새로고침이 왕복 중에 같은 줄을 다시 그려도 그 버튼은 죽어
    # 있어야 합니다 — 살아나면 두 번째 클릭이 그 자리에서 다시 열립니다.
    assert "disabled" in out["duringFlight"], out["duringFlight"]


@js
def test_a_failed_cancel_can_be_tried_again():
    """실패까지 잠가 버리면 취소할 방법이 사라집니다."""
    out = run_js("""
  const btn = button("man_abc");
  const first = cancelPending(btn);
  failAll("네트워크가 끊겼습니다");
  await first;
  const after = {disabled: btn.disabled, text: btn.textContent, msg: MSG};
  const again = cancelPending(btn);
  settleAll({cancelled: "man_abc"});
  await again;
  return {after: after, posts: POSTS.length, finalMsg: MSG};
""")
    assert out["after"]["disabled"] is False
    assert out["after"]["msg"]["kind"] == "err"
    assert out["posts"] == 2, "실패한 뒤에는 다시 보낼 수 있어야 합니다"
    assert out["finalMsg"]["kind"] == "ok"
