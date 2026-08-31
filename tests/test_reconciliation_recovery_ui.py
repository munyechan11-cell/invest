"""격리된 Toss 실거래 실행은 사람이 대조한 뒤에만 보존 처리합니다.

이 검사는 실제 ``index.html`` 스크립트를 통째로 JavaScript 엔진에 태웁니다.
정상·실패·지연 응답을 가짜 서버로 만들고, 선택 전략과 로그인 사용자가 바뀐
뒤 늦게 도착한 응답이 다른 계정의 DOM을 덮지 않는지까지 확인합니다.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_pnl_and_tradelog_are_split_by_mode import (
    DOM,
    SCRIPT,
    STATIC,
    _engine,
)

DRIVER = r"""
var __requests2 = [];
var __holds2 = [];
var __network2 = null;

function __response2(spec) {
  var status = spec && spec.status !== undefined ? spec.status : 200;
  var body = spec && spec.body !== undefined ? spec.body : spec;
  return {
    ok: status >= 200 && status < 300,
    status: status,
    json: function () { return Promise.resolve(body); },
    text: function () { return Promise.resolve(JSON.stringify(body)); },
    headers: {get: function () { return "application/json"; }}
  };
}

fetch = function (url, options) {
  options = options || {};
  var request = {
    url: String(url),
    method: options.method || "GET",
    body: options.body || ""
  };
  __requests2.push(request);
  var spec = __network2 ? __network2(request) : undefined;
  if (spec === undefined) return new Promise(function () {});
  if (spec && Object.prototype.hasOwnProperty.call(spec, "hold")) {
    return new Promise(function (resolve) {
      __holds2.push(function () { resolve(__response2(spec.hold)); });
    });
  }
  return Promise.resolve(__response2(spec));
};
window.fetch = fetch;

function __settle2() {
  return (async function () {
    for (var i = 0; i < 16; i++) await Promise.resolve();
  })();
}
function __el2(id) { return document.getElementById(id); }
function __strategy2(id) {
  return {id: id, name: "strategy-" + id, label_ko: "복구 전략 " + id,
          mode: "live", broker: "toss", requires: [], signals: [], guards: [],
          execution: {}, protections: [], tickers: [], symbols: 1};
}
var __phrases2 = {
  open_orders: "토스 앱 미체결 없음 확인",
  today_fills: "토스 앱 당일 체결 대조 완료",
  holdings: "토스 앱 보유 수량 대조 완료",
  cash: "토스 앱 현금 대조 완료",
  daily_loss: "토스 앱 당일 손실 대조 완료"
};
var __ack2 = "기존 실행을 보존하고 새 실행으로 시작합니다";
function __required2(runId, configPath, botRunning) {
  return {
    required: true,
    run: {id: runId, strategy: "strategy-" + configPath, mode: "live",
          requires_reconciliation: true, archived_at: null, required: true},
    bot_running: botRunning,
    confirmation_phrases: __phrases2,
    acknowledgement_phrase: __ack2,
    recovery_instructions: "이전 실거래 실행이 안전 종료를 완료하지 못했습니다."
  };
}
function __fill2() {
  RECONCILIATION_FIELDS.forEach(function (row) {
    __el2(row[1]).checked = true;
  });
  __el2("reconciliationReason").value =
    "토스 앱에서 실제 계좌의 다섯 항목을 모두 대조했습니다";
  __el2("reconciliationAck").value = __ack2;
  syncReconciliationSubmit();
}
function __releaseAll2() {
  var releases = __holds2.splice(0);
  releases.forEach(function (release) { release(); });
  return releases.length;
}

var __out2 = {};
(async function () {
  var out = __out2;
  try {
    var realStart = start;
    start = function () { started = true; };
    signedIn({id: "A", email: "a@example.test", display_name: "A"});
    strategies = [__strategy2("toss-a")];
    __el2("strategyPick").value = "toss-a";

    // 정상 계정에는 복구 카드가 열리지 않습니다.
    __network2 = function (request) {
      if (request.url.indexOf("/api/trader/reconciliation?") === 0) {
        return {body: {required: false, run: null, bot_running: false,
          confirmation_phrases: __phrases2,
          acknowledgement_phrase: __ack2,
          recovery_instructions: "안내"}};
      }
      return undefined;
    };
    await loadReconciliationStatus();
    out.clean_hidden = __el2("reconciliationCard").hidden;

    // required:true만 카드에 올리고 서버가 준 문구를 그대로 표시합니다.
    __network2 = function (request) {
      if (request.url.indexOf("/api/trader/reconciliation?") === 0) {
        return {body: __required2(41, "toss-a", false)};
      }
      return undefined;
    };
    __requests2.length = 0;
    await loadReconciliationStatus();
    out.required_hidden = __el2("reconciliationCard").hidden;
    out.required_run = __el2("reconciliationRunId").textContent;
    out.required_strategy = __el2("reconciliationStrategy").textContent;
    out.required_mode = __el2("reconciliationMode").textContent;
    out.required_instructions = __el2("reconciliationInstructions").textContent;
    out.required_phrases = RECONCILIATION_FIELDS.map(function (row) {
      return __el2(row[2]).textContent;
    });
    out.required_ack = __el2("reconciliationAckPhrase").textContent;
    out.required_gets = __requests2.map(function (r) { return r.url; });
    out.initially_disabled = __el2("reconciliationSubmit").disabled;
    __fill2();
    out.valid_enabled = !__el2("reconciliationSubmit").disabled;

    // 첫 POST를 잡아 둔 동안 두 번째 호출은 서버로 나가면 안 됩니다.
    __network2 = function (request) {
      if (request.url === "/api/trader/reconciliation/archive") {
        return {hold: {body: {archived: true, idempotent: false, run_id: 41,
          strategy: "strategy-toss-a", mode: "live", archived_at: "now",
          fresh_run_on_next_start: true,
          next_start_allowed_at: "2026-08-31T15:00:00+00:00",
          message: "기존 실행과 감사 기록을 보존했습니다. 당일 한도 초기화를 막기 " +
            "위해 오늘은 새 실거래를 시작할 수 없습니다. 표시된 다음 시작 가능 " +
            "시각 이후 새 실행으로 시작하세요."}}};
      }
      return undefined;
    };
    __requests2.length = 0;
    var firstSubmit = __el2("reconciliationSubmit").onclick();
    await __settle2();
    var secondSubmit = __el2("reconciliationSubmit").onclick();
    await __settle2();
    out.pending_disabled = __el2("reconciliationSubmit").disabled;
    out.submit_requests_while_held = __requests2.filter(function (r) {
      return r.url === "/api/trader/reconciliation/archive";
    }).length;
    out.submitted_body = JSON.parse(__requests2.filter(function (r) {
      return r.url === "/api/trader/reconciliation/archive";
    })[0].body);
    __releaseAll2();
    await Promise.all([firstSubmit, secondSubmit]);
    await __settle2();
    out.success_message = __el2("reconciliationMsg").textContent;
    out.success_reason = __el2("reconciliationReason").value;
    out.success_ack = __el2("reconciliationAck").value;
    out.success_checks = RECONCILIATION_FIELDS.map(function (row) {
      return [__el2(row[1]).checked, __el2(row[1]).disabled];
    });
    out.success_resolved = __el2("reconciliationCard").classList.contains("resolved");
    out.start_requests = __requests2.filter(function (r) {
      return r.url.indexOf("/api/trader/start") === 0;
    }).length;

    // 봇이 켜져 있으면 모든 확인을 채워도 제출을 열지 않습니다.
    __network2 = function (request) {
      if (request.url.indexOf("/api/trader/reconciliation?") === 0) {
        return {body: __required2(42, "toss-a", true)};
      }
      return undefined;
    };
    await loadReconciliationStatus();
    __fill2();
    out.running_disabled = __el2("reconciliationSubmit").disabled;
    out.running_message = __el2("reconciliationMsg").textContent;

    // 서버가 거절한 정확한 이유를 숨기지 않고 다시 시도 가능한 상태로 둡니다.
    __network2 = function (request) {
      if (request.url.indexOf("/api/trader/reconciliation?") === 0) {
        return {body: __required2(43, "toss-a", false)};
      }
      return undefined;
    };
    await loadReconciliationStatus();
    __fill2();
    var messageFocused = false;
    __el2("reconciliationMsg").focus = function () { messageFocused = true; };
    __network2 = function (request) {
      if (request.url === "/api/trader/reconciliation/archive") {
        return {status: 409, body: {error: "복구 대상 실행이 바뀌었습니다"}};
      }
      return undefined;
    };
    await __el2("reconciliationSubmit").onclick();
    out.failure_message = __el2("reconciliationMsg").textContent;
    out.failure_focused = messageFocused;
    out.failure_retry_enabled = !__el2("reconciliationSubmit").disabled;

    // 2xx여도 다음 허용 시각이 없으면 성공으로 그리지 않습니다.
    __network2 = function (request) {
      if (request.url.indexOf("/api/trader/reconciliation?") === 0) {
        return {body: __required2(44, "toss-a", false)};
      }
      return undefined;
    };
    await loadReconciliationStatus();
    __fill2();
    __network2 = function (request) {
      if (request.url === "/api/trader/reconciliation/archive") {
        return {body: {archived: true, fresh_run_on_next_start: true,
          message: "보존했습니다"}};
      }
      return undefined;
    };
    await __el2("reconciliationSubmit").onclick();
    out.malformed_success_message = __el2("reconciliationMsg").textContent;
    out.malformed_success_resolved =
      __el2("reconciliationCard").classList.contains("resolved");
    out.malformed_success_retry_enabled = !__el2("reconciliationSubmit").disabled;

    // 전략 A의 GET이 늦게 와도 이미 선택한 전략 B를 덮지 않습니다.
    strategies = [__strategy2("toss-a"), __strategy2("toss-b")];
    __el2("strategyPick").value = "toss-a";
    __network2 = function (request) {
      if (request.url.indexOf("config_path=toss-a") >= 0) {
        return {hold: {body: __required2(101, "toss-a", false)}};
      }
      if (request.url.indexOf("config_path=toss-b") >= 0) {
        return {body: __required2(202, "toss-b", false)};
      }
      return undefined;
    };
    var oldStrategyGet = loadReconciliationStatus();
    await __settle2();
    var oldStrategyReleases = __holds2.splice(0);
    __el2("strategyPick").value = "toss-b";
    await loadReconciliationStatus();
    oldStrategyReleases.forEach(function (release) { release(); });
    await oldStrategyGet;
    await __settle2();
    out.strategy_race_run = __el2("reconciliationRunId").textContent;
    out.strategy_race_strategy = __el2("reconciliationStrategy").textContent;

    // A GET pending -> logout -> B login -> A resolve. A의 run은 B에 못 옵니다.
    strategies = [__strategy2("toss-a")];
    __el2("strategyPick").value = "toss-a";
    __network2 = function (request) {
      if (request.url.indexOf("config_path=toss-a") >= 0) {
        return {hold: {body: __required2(501, "toss-a", false)}};
      }
      return undefined;
    };
    var oldUserGet = loadReconciliationStatus();
    await __settle2();
    var oldUserGetReleases = __holds2.splice(0);
    signedOut();
    out.logout_hidden = __el2("reconciliationCard").hidden;
    out.logout_run = __el2("reconciliationRunId").textContent;
    out.logout_instructions = __el2("reconciliationInstructions").textContent;
    out.logout_ack_phrase = __el2("reconciliationAckPhrase").textContent;
    out.logout_reason = __el2("reconciliationReason").value;
    out.logout_ack = __el2("reconciliationAck").value;
    out.logout_message = __el2("reconciliationMsg").textContent;
    signedIn({id: "B", email: "b@example.test", display_name: "B"});
    strategies = [__strategy2("toss-b")];
    __el2("strategyPick").value = "toss-b";
    __network2 = function (request) {
      if (request.url.indexOf("config_path=toss-b") >= 0) {
        return {body: __required2(601, "toss-b", false)};
      }
      return undefined;
    };
    await loadReconciliationStatus();
    oldUserGetReleases.forEach(function (release) { release(); });
    await oldUserGet;
    await __settle2();
    out.user_get_race_run = __el2("reconciliationRunId").textContent;
    out.user_get_race_strategy = __el2("reconciliationStrategy").textContent;

    // B POST pending -> logout -> C login -> B resolve. B의 성공 문구와 입력도
    // C 화면에 못 옵니다.
    __fill2();
    __network2 = function (request) {
      if (request.url === "/api/trader/reconciliation/archive") {
        return {hold: {body: {archived: true, run_id: 601,
          fresh_run_on_next_start: true}}};
      }
      return undefined;
    };
    var oldUserPost = __el2("reconciliationSubmit").onclick();
    await __settle2();
    var oldUserPostReleases = __holds2.splice(0);
    signedOut();
    signedIn({id: "C", email: "c@example.test", display_name: "C"});
    strategies = [__strategy2("toss-c")];
    __el2("strategyPick").value = "toss-c";
    __network2 = function (request) {
      if (request.url.indexOf("config_path=toss-c") >= 0) {
        return {body: {required: false, run: null, bot_running: false,
          confirmation_phrases: __phrases2,
          acknowledgement_phrase: __ack2, recovery_instructions: "안내"}};
      }
      return undefined;
    };
    await loadReconciliationStatus();
    oldUserPostReleases.forEach(function (release) { release(); });
    await oldUserPost;
    await __settle2();
    out.user_post_race_hidden = __el2("reconciliationCard").hidden;
    out.user_post_race_run = __el2("reconciliationRunId").textContent;
    out.user_post_race_reason = __el2("reconciliationReason").value;
    out.user_post_race_ack = __el2("reconciliationAck").value;
    out.user_post_race_message = __el2("reconciliationMsg").textContent;
    start = realStart;
  } catch (error) {
    out.crash = String(error && error.stack || error);
  }
  __say("<<<RECOVERY" + JSON.stringify(out) + "RECOVERY>>>");
})();
"""


@pytest.fixture(scope="module")
def screen() -> dict:
    engine = _engine()
    if engine is None:
        pytest.skip("JavaScript 엔진이 없습니다")
    chart = (STATIC / "chart.js").read_text(encoding="utf-8")
    program = "\n".join([DOM, chart, SCRIPT, DRIVER])
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(program)
        path = Path(handle.name)
    try:
        proc = subprocess.run(
            [engine, str(path)], capture_output=True, text=True, timeout=120
        )
    finally:
        path.unlink(missing_ok=True)
    match = re.search(r"<<<RECOVERY(.*?)RECOVERY>>>", proc.stdout, re.S)
    assert match, (
        "복구 UI 시나리오가 끝나지 않았습니다 "
        f"(rc={proc.returncode}):\n{(proc.stdout + proc.stderr)[:4000]}"
    )
    result = json.loads(match.group(1))
    assert "crash" not in result, result.get("crash")
    return result


def test_clean_state_stays_hidden_and_required_state_is_explained(screen):
    assert screen["clean_hidden"] is True
    assert screen["required_hidden"] is False
    assert screen["required_run"] == "41"
    assert screen["required_strategy"] == "strategy-toss-a"
    assert screen["required_mode"] == "실거래"
    assert "안전 종료" in screen["required_instructions"]
    assert screen["required_phrases"] == list({
        "open_orders": "토스 앱 미체결 없음 확인",
        "today_fills": "토스 앱 당일 체결 대조 완료",
        "holdings": "토스 앱 보유 수량 대조 완료",
        "cash": "토스 앱 현금 대조 완료",
        "daily_loss": "토스 앱 당일 손실 대조 완료",
    }.values())
    assert screen["required_ack"] == "기존 실행을 보존하고 새 실행으로 시작합니다"
    assert screen["required_gets"] == [
        "/api/trader/reconciliation?config_path=toss-a"
    ]
    assert screen["initially_disabled"] is True
    assert screen["valid_enabled"] is True


def test_submit_is_exact_single_and_never_starts_the_bot(screen):
    assert screen["pending_disabled"] is True
    assert screen["submit_requests_while_held"] == 1
    assert screen["submitted_body"] == {
        "config_path": "toss-a",
        "run_id": 41,
        "reason": "토스 앱에서 실제 계좌의 다섯 항목을 모두 대조했습니다",
        "confirmations": {
            "open_orders": "토스 앱 미체결 없음 확인",
            "today_fills": "토스 앱 당일 체결 대조 완료",
            "holdings": "토스 앱 보유 수량 대조 완료",
            "cash": "토스 앱 현금 대조 완료",
            "daily_loss": "토스 앱 당일 손실 대조 완료",
        },
        "acknowledgement": "기존 실행을 보존하고 새 실행으로 시작합니다",
    }
    assert screen["start_requests"] == 0


def test_success_preserves_history_starts_fresh_and_zeroizes_inputs(screen):
    message = screen["success_message"]
    assert "기존 실행과 감사 기록을 보존" in message
    assert "오늘은 새 실거래를 시작할 수 없습니다" in message
    assert "새 실행으로 시작" in message
    assert "2026년 9월 1일" in message
    assert "한국 시간" in message
    assert "자동매매는 시작하지 않았습니다" in message
    assert screen["success_reason"] == ""
    assert screen["success_ack"] == ""
    assert screen["success_checks"] == [[False, True]] * 5
    assert screen["success_resolved"] is True


def test_running_bot_blocks_submit_and_server_failure_is_visible(screen):
    assert screen["running_disabled"] is True
    assert "봇이 실행 중" in screen["running_message"]
    assert "복구 대상 실행이 바뀌었습니다" in screen["failure_message"]
    assert screen["failure_focused"] is True
    assert screen["failure_retry_enabled"] is True


def test_malformed_archive_success_is_not_rendered_as_success(screen):
    assert "다음 시작 가능 시각을 확인하지 못했습니다" in screen[
        "malformed_success_message"
    ]
    assert screen["malformed_success_resolved"] is False
    assert screen["malformed_success_retry_enabled"] is True


def test_late_strategy_response_cannot_replace_current_strategy(screen):
    assert screen["strategy_race_run"] == "202"
    assert screen["strategy_race_strategy"] == "strategy-toss-b"


def test_logout_zeroizes_recovery_dom_and_late_get_cannot_cross_accounts(screen):
    assert screen["logout_hidden"] is True
    for key in (
        "logout_run", "logout_instructions", "logout_ack_phrase",
        "logout_reason", "logout_ack", "logout_message",
    ):
        assert screen[key] == "", f"{key}: {screen[key]!r}"
    assert screen["user_get_race_run"] == "601"
    assert screen["user_get_race_strategy"] == "strategy-toss-b"


def test_late_post_success_cannot_render_in_the_next_account(screen):
    assert screen["user_post_race_hidden"] is True
    for key in (
        "user_post_race_run", "user_post_race_reason",
        "user_post_race_ack", "user_post_race_message",
    ):
        assert screen[key] == "", f"{key}: {screen[key]!r}"


def test_recovery_source_has_no_automatic_start_path():
    handler = SCRIPT[
        SCRIPT.index('$("#reconciliationSubmit").onclick'):
        SCRIPT.index("/* 이 전략을 사람이 부르는 이름")
    ]
    assert "/api/trader/reconciliation/archive" in handler
    assert "/api/trader/start" not in handler
