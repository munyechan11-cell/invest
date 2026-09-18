""""연동됨" 이라고 써 놓고 잔고 탭에서는 "연동한 증권사가 없습니다".

설정 화면의 「한국투자증권 모의투자」 카드는 **연동됨** 이었습니다. 같은
계정으로 「내 계좌」 를 열면 이렇게 나왔습니다:

    ● 계좌 조회 미지원
    아직 연동한 증권사가 없습니다. ⚙ 설정에서 증권사를 연결하면…

두 화면이 같은 계정을 두고 서로 다른 말을 합니다. 그리고 두 번째 문장은
사용자에게 **하지 않아도 되는 일** 을 시킵니다 — 이미 한 일을.

원인은 한 줄짜리였습니다. 한투를 모의투자/실계좌 둘로 쪼갤 때 잔고를 볼
증권사를 고르는 표(`_ACCOUNT_VENUES`)에 `kis_paper` 를 안 넣었습니다.
"""
from __future__ import annotations

from quant.live.credentials import VENUES_BY_ID
from quant.webapp.registry import (
    _ACCOUNT_VENUES,
    _account_adapter,
    _connected_account_venue,
)

PAPER = {"KIS_PAPER_APP_KEY": "k", "KIS_PAPER_APP_SECRET": "s",
         "KIS_PAPER_ACCOUNT_NO": "50206221"}
REAL = {"KIS_APP_KEY": "rk", "KIS_APP_SECRET": "rs", "KIS_ACCOUNT_NO": "12345678"}
QUOTES_ONLY = {"KIS_APP_KEY": "rk", "KIS_APP_SECRET": "rs"}


# ── 고르기 ───────────────────────────────────────────────────────────────
def test_a_paper_only_connection_is_an_account():
    assert _connected_account_venue(PAPER) == "kis_paper"


def test_quote_keys_alone_are_not_an_account():
    """시세 때문에 앱 키만 넣은 사람이 있습니다 — 그 사람에게 실계좌 잔고를
    조회하러 가면 계좌번호가 없어 어댑터가 생성자에서 터집니다."""
    assert _connected_account_venue(QUOTES_ONLY) == ""
    assert _connected_account_venue({**PAPER, **QUOTES_ONLY}) == "kis_paper"


def test_a_full_real_setup_wins():
    """계좌번호까지 넣었다는 것은 실계좌를 쓰겠다는 뜻입니다."""
    assert _connected_account_venue({**PAPER, **REAL}) == "kis"


def test_nothing_connected_is_still_nothing():
    assert _connected_account_venue({}) == ""


# ── 고른 것을 어댑터로 옮기기 ────────────────────────────────────────────
def test_the_venue_id_is_translated_into_an_adapter():
    """`kis_paper` 는 거래소 id 이지 어댑터 이름이 아닙니다 — 그대로 넣으면
    "그런 브로커 없음" 으로 터집니다."""
    kind, params = _account_adapter("kis_paper")
    assert kind == "kis" and params["environment"] == "paper"


def test_the_real_account_is_read_from_the_live_host():
    kind, params = _account_adapter("kis")
    assert kind == "kis" and params["environment"] == "live"


def test_the_environment_is_never_left_to_chance():
    """`environment` 가 비면 어댑터가 `mode` 로 추론합니다. 조회 경로는
    `DRY_RUN` 으로 낮춰 세우므로 그 추론은 **언제나 모의** 가 되고, 실계좌를
    연동한 사람이 모의투자 잔고를 자기 잔고로 읽게 됩니다."""
    for venue, _needed, kind, params in _ACCOUNT_VENUES:
        if kind == "kis":
            assert params.get("environment") in ("paper", "live"), venue


def test_every_account_venue_has_a_card_to_point_at():
    """`via_connected_venue` 가 이 id 로 한국어 이름을 찾습니다. 없으면
    화면이 "kis_paper 계좌입니다" 라고 씁니다."""
    for venue, _needed, _kind, _params in _ACCOUNT_VENUES:
        assert venue in VENUES_BY_ID, f"{venue} 에 대응하는 설정 카드가 없습니다"


def test_the_required_keys_match_that_cards_required_fields():
    """표와 카드가 다른 키를 요구하면, 카드는 "연동됨" 인데 잔고는 "없음"
    이 됩니다 — 이 버그가 정확히 그 모양이었습니다."""
    for venue, needed, _kind, _params in _ACCOUNT_VENUES:
        card = {env for env, _label, required in VENUES_BY_ID[venue].fields
                if required}
        assert card <= set(needed), (
            f"{venue}: 카드는 {sorted(card)} 를 필수로 받는데 잔고 조회는 "
            f"{sorted(needed)} 를 봅니다")


# ── "연동했는데 왜 없다고 하지" ──────────────────────────────────────────
#
# 실계좌 카드의 계좌번호를 선택으로 바꾸면서 같은 모양의 구멍이 하나 더
# 생겼습니다. 시세 때문에 앱 키만 넣은 사람은 카드에서 **연동됨** 을 보고,
# 잔고 탭에서 "아직 연동한 증권사가 없습니다" 를 읽습니다 — 이미 한 일을
# 다시 하러 가고, 두 번째에도 같은 화면을 봅니다.

def test_a_half_filled_card_is_not_called_empty():
    from quant.webapp.registry import _account_gap

    said = _account_gap(QUOTES_ONLY)
    assert "한국투자증권 실계좌" in said and "연동돼 있지만" in said
    assert "계좌번호" in said, "무엇이 비었는지가 없습니다"


def test_it_says_leaving_it_alone_is_also_fine():
    """시세용으로만 넣은 사람에게는 **채우지 않는 것도 정답** 입니다."""
    from quant.webapp.registry import _account_gap

    said = _account_gap(QUOTES_ONLY)
    assert "그대로 두셔도" in said and "모의투자" in said


def test_nothing_connected_gets_no_such_excuse():
    """아무것도 안 넣은 사람에게 "연동돼 있지만" 이라고 하면 거짓말입니다."""
    from quant.webapp.registry import _account_gap

    assert _account_gap({}) == ""


def test_a_complete_connection_has_no_gap():
    from quant.webapp.registry import _account_gap

    assert _account_gap(PAPER) == ""
    assert _account_gap(REAL) == ""
