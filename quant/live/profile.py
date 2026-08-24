"""투자 성향 진단 — 8문항으로 시작 설정을 잡는다.

MBTI 식으로 네 축을 재는데, 각 축이 **엔진의 실제 파라미터에 직접 연결되어** 있는
것이 핵심입니다. 성향 검사가 흔히 실패하는 이유는 결과가 듣기 좋은 라벨에서 끝나기
때문입니다. 여기서는 축 점수가 곧 목표 변동성이고, 손절 폭이고, 하루 손실 한도입니다.

    R  위험 성향     방어 ←────────→ 공격     사이즈 · 목표변동성 · 종목수
    H  매매 주기     단타 ←────────→ 장기     봉 주기 · 보유기간 · 알파 선택
    E  판단 근거     규칙 ←────────→ 재량     AI 데스크 사용 여부와 비중
    C  개입 성향     위임 ←────────→ 직접     알림 · 수동 고정 · 일시정지 기본값

**정직하게 말해 둘 것 두 가지.**

첫째, 여덟 문항으로 위험 감내도를 잴 수는 없습니다. 이건 과학적 진단이 아니라
**출발점**입니다. 진짜 제약은 "하루에 얼마까지 잃어도 생활에 지장이 없는가"이고,
그건 설문이 아니라 본인만 아는 숫자라 마지막 문항에서 직접 묻습니다.

둘째, 성향은 **안전장치를 약화시키지 못합니다.** 아무리 공격형으로 나와도 손절은
사라지지 않고, 일일 손실 한도는 0이 되지 않으며, 레버리지는 1배를 넘지 않습니다.
공격형이 뜻하는 것은 "같은 규칙 안에서 더 크게"이지 "규칙 없이"가 아닙니다.
그래서 모든 파생값에는 바닥과 천장이 있습니다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from quant.core.types import UTC

log = logging.getLogger("quant.profile")

Axis = Literal["R", "H", "E", "C"]

AXIS_META: dict[str, dict] = {
    "R": {"name": "위험 성향", "low": "방어형", "high": "공격형",
          "affects": "포지션 크기 · 목표 변동성 · 동시 보유 종목 수 · 손절 폭"},
    "H": {"name": "매매 주기", "low": "단기", "high": "장기",
          "affects": "봉 주기 · 보유 기간 · 사용하는 알파 모델"},
    "E": {"name": "판단 근거", "low": "규칙 중심", "high": "재량·AI 활용",
          "affects": "AI 데스크 사용 여부와 확신도 문턱"},
    "C": {"name": "개입 성향", "low": "완전 위임", "high": "직접 개입",
          "affects": "알림 빈도 · 수동 매수 고정 · 시작 시 일시정지 여부"},
}


@dataclass
class Option:
    id: str
    text: str
    #: axis -> delta in [-2, +2]
    weights: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text}


@dataclass
class Question:
    id: str
    text: str
    options: list[Option]
    hint: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "hint": self.hint,
                "options": [o.to_dict() for o in self.options]}


QUESTIONS: list[Question] = [
    Question(
        "q1", "보유 종목이 하루 만에 10% 빠졌습니다. 손절선에는 아직 안 닿았습니다.",
        [
            Option("a", "바로 정리한다. 더 빠질 수 있다", {"R": -2, "C": +1}),
            Option("b", "손절선까지는 그냥 둔다. 정해둔 규칙이니까", {"R": 0, "E": -1}),
            Option("c", "오히려 더 산다. 싸졌으니까", {"R": +2, "C": +1}),
            Option("d", "왜 빠졌는지 먼저 찾아보고 정한다", {"E": +1, "C": +1}),
        ],
        "정답은 없습니다. b 가 규칙 기반, c 가 공격형에 가깝습니다.",
    ),
    Question(
        "q2", "1년 뒤 이 계좌를 어떻게 하고 싶으신가요?",
        [
            Option("a", "원금은 무조건 지키고 예금보다 조금 낫기만 하면 된다", {"R": -2}),
            Option("b", "연 10~20% 정도면 만족한다", {"R": -0.5}),
            Option("c", "연 30% 이상 노린다. 반토막도 감수한다", {"R": +2}),
            Option("d", "수익률보다 시장을 이기는 게 목표다", {"R": +0.5, "E": -1}),
        ],
    ),
    Question(
        "q3", "얼마나 자주 계좌를 확인하실 것 같나요?",
        [
            Option("a", "하루에도 몇 번씩", {"H": -1.5, "C": +2}),
            Option("b", "하루 한 번쯤", {"H": -0.5, "C": +0.5}),
            Option("c", "주에 한두 번", {"H": +1, "C": -0.5}),
            Option("d", "한 달에 한 번이면 충분하다", {"H": +2, "C": -2}),
        ],
        "자주 볼수록 짧은 봉이 맞습니다. 다만 자주 보는 것과 잘 하는 것은 다릅니다.",
    ),
    Question(
        "q4", "매매 판단은 무엇에 더 기대고 싶으신가요?",
        [
            Option("a", "숫자와 규칙. 백테스트로 검증된 것만", {"E": -2}),
            Option("b", "규칙이 기본, 이상하면 내가 개입", {"E": -0.5, "C": +1}),
            Option("c", "AI가 뉴스·수급까지 읽고 판단해 주면 좋겠다", {"E": +2}),
            Option("d", "AI 의견은 참고만, 결정은 규칙대로", {"E": +0.5}),
        ],
    ),
    Question(
        "q5", "동시에 몇 종목까지 들고 계시겠어요?",
        [
            Option("a", "1~2개. 확실한 것에 집중", {"R": +1.5}),
            Option("b", "3~5개", {"R": 0}),
            Option("c", "6~10개로 분산", {"R": -1}),
            Option("d", "많을수록 좋다. 분산이 최고", {"R": -2}),
        ],
        "집중은 공격, 분산은 방어입니다. 종목이 적을수록 한 번의 실수가 커집니다.",
    ),
    Question(
        "q6", "자동매매가 사흘 연속 손실을 냈습니다.",
        [
            Option("a", "즉시 끈다", {"R": -2, "C": +1.5}),
            Option("b", "한도까지는 지켜본다. 그러라고 만든 한도니까", {"R": 0, "E": -1}),
            Option("c", "설정을 손본다", {"C": +1.5, "E": +0.5}),
            Option("d", "원래 그런 구간이 있다. 그냥 둔다", {"R": +1.5, "C": -1.5}),
        ],
    ),
    Question(
        "q7", "어떤 방식이 더 끌리시나요?",
        [
            Option("a", "오를 때 따라 타는 추세추종", {"H": +1, "R": +0.5}),
            Option("b", "빠졌을 때 사는 눌림목·역발상", {"H": -0.5, "R": +0.5}),
            Option("c", "외국인·기관 수급을 따라가기", {"H": +0.5, "E": +0.5}),
            Option("d", "여러 방식을 섞어서", {"E": -0.5}),
        ],
    ),
    Question(
        "q8", "**하루에 최대 얼마까지 잃어도 생활에 지장이 없나요?** (자산 대비)",
        [
            Option("a", "0.5% — 거의 잃고 싶지 않다", {"R": -2}),
            Option("b", "1~2%", {"R": -0.5}),
            Option("c", "3~5%", {"R": +1}),
            Option("d", "5% 이상도 괜찮다", {"R": +2}),
        ],
        "이 문항만은 성향이 아니라 사실을 묻는 것입니다. 나머지 일곱 개보다 "
        "이 하나가 실제 손실을 더 정확히 결정합니다.",
    ),
]

QUESTIONS_BY_ID = {q.id: q for q in QUESTIONS}


def _axis_maxima() -> dict[str, float]:
    """축별로 얻을 수 있는 최대 절대점수.

    축마다 관여하는 문항 수가 다릅니다 — 위험(R)은 여섯 문항이 건드리지만 주기(H)는
    두 개뿐입니다. 고정된 값으로 나누면 문항이 적은 축은 아무리 극단으로 답해도
    중간 근처에 머물러, 결과가 사용자의 답을 배신합니다.
    """
    maxima = dict.fromkeys(AXIS_META, 0.0)
    for question in QUESTIONS:
        for axis in maxima:
            reach = max((abs(o.weights.get(axis, 0.0)) for o in question.options),
                        default=0.0)
            maxima[axis] += reach
    return {axis: value or 1.0 for axis, value in maxima.items()}


AXIS_MAX = _axis_maxima()

#: 축 조합별 이름. 점수가 실제 값을 정하고, 이름은 부르기 위한 것입니다.
ARCHETYPES: dict[str, tuple[str, str]] = {
    "RHEC": ("불도저", "크게 걸고 길게 들고 가며, AI 의견까지 직접 챙기는 유형"),
    "RHEc": ("항해사", "장기 추세를 AI와 함께 읽되 시스템에 맡기는 유형"),
    "RHeC": ("사냥꾼", "규칙대로 크게 베팅하고 직접 개입하는 유형"),
    "RHec": ("등대지기", "규칙 기반 장기 집중 투자를 시스템에 맡기는 유형"),
    "RhEC": ("스캘퍼", "짧게 자주, 크게, AI와 함께, 직접 손대는 유형"),
    "RhEc": ("포수", "짧은 주기에서 AI 판단으로 크게 베팅하는 유형"),
    "RheC": ("돌격병", "규칙 기반 단타를 크게 하며 직접 챙기는 유형"),
    "Rhec": ("기계공", "규칙 기반 단기 집중 매매를 전부 위임하는 유형"),
    "rHEC": ("정원사", "천천히 안전하게, AI 참고, 직접 살피는 유형"),
    "rHEc": ("농부", "길게 분산해 두고 AI에게 맡겨 두는 유형"),
    "rHeC": ("건축가", "규칙대로 길게 분산하며 직접 점검하는 유형"),
    "rHec": ("수도승", "규칙 기반 장기 분산을 완전히 위임하는 유형"),
    "rhEC": ("파수꾼", "짧게 보되 조심스럽게, AI 참고, 직접 개입", ),
    "rhEc": ("관측자", "짧은 주기를 조심스럽게 AI와 함께 위임하는 유형"),
    "rheC": ("회계사", "규칙 기반 단기 분산, 직접 확인하는 유형"),
    "rhec": ("자동항법", "규칙 기반 단기 분산을 전부 시스템에 맡기는 유형"),
}


@dataclass
class InvestorProfile:
    scores: dict[str, float] = field(default_factory=lambda: {"R": 0, "H": 0, "E": 0, "C": 0})
    answers: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""
    #: 사용자가 진단 결과를 직접 덮어쓴 값
    overrides: dict[str, float] = field(default_factory=dict)

    # ── 정규화 ───────────────────────────────────────────────────────────
    def normalized(self, axis: str) -> float:
        """축 점수를 -1..+1 로 정규화. 진단을 안 했으면 0(중립)."""
        if axis in self.overrides:
            return max(-1.0, min(1.0, self.overrides[axis]))
        raw = self.scores.get(axis, 0.0)
        return max(-1.0, min(1.0, raw / AXIS_MAX.get(axis, 1.0)))

    @property
    def code(self) -> str:
        if not self.completed:
            return "----"
        return "".join(
            axis.upper() if self.normalized(axis) >= 0 else axis.lower()
            for axis in ("R", "H", "E", "C")
        )

    @property
    def name(self) -> str:
        # 진단하지 않은 사람에게 유형 이름을 붙이지 않는다. 모든 축이 0 이면
        # 부호 규칙상 가장 공격적인 조합이 나오는데, 그건 기본값으로 삼기에
        # 최악의 라벨이다.
        if not self.completed:
            return "미진단"
        return ARCHETYPES.get(self.code, ("균형형", "중립적인 유형"))[0]

    @property
    def description(self) -> str:
        if not self.completed:
            return "아직 진단하지 않았습니다 — 중립 기본값으로 동작합니다"
        return ARCHETYPES.get(self.code, ("균형형", "중립적인 유형"))[1]

    @property
    def completed(self) -> bool:
        return len(self.answers) >= len(QUESTIONS) or bool(self.overrides)

    def axis_summary(self) -> list[dict]:
        out = []
        for axis, meta in AXIS_META.items():
            value = self.normalized(axis)
            out.append({
                "axis": axis, "name": meta["name"],
                "low": meta["low"], "high": meta["high"],
                "value": round(value, 3),
                "label": meta["high"] if value >= 0.25
                         else meta["low"] if value <= -0.25 else "중간",
                "affects": meta["affects"],
                "overridden": axis in self.overrides,
            })
        return out

    # ── 엔진 설정으로의 번역 ─────────────────────────────────────────────
    def settings(self) -> dict:
        """축 점수를 실제 엔진 파라미터로 옮긴다.

        모든 값에 바닥과 천장이 있습니다. 가장 공격적인 답을 몰아서 해도 손절은
        남고, 하루 손실 한도는 0이 되지 않으며, 총 노출은 1배를 넘지 않습니다.
        성향은 "같은 규칙 안에서 더 크게"까지만 바꿉니다.
        """
        r, h, e, c = (self.normalized(a) for a in ("R", "H", "E", "C"))

        def lerp(low: float, high: float, t: float) -> float:
            return low + (high - low) * (t + 1) / 2

        return {
            # ── 사이징 ────────────────────────────────────────────────
            "target_annual_vol": round(lerp(0.08, 0.30, r), 3),
            "max_position_weight": round(lerp(0.12, 0.40, r), 3),
            "max_gross_leverage": round(lerp(0.60, 1.00, r), 3),  # 1배 초과 없음
            "max_positions": int(round(lerp(8, 2, r))),           # 공격일수록 집중
            "cash_reserve_pct": round(lerp(0.15, 0.02, r), 3),

            # ── 손절 ──────────────────────────────────────────────────
            # 공격형일수록 손절을 *넓게* 둡니다. 좁은 손절은 안전이 아니라
            # 잦은 손실이며, 보유기간이 만드는 소음보다 좁으면 맞는 판단도 잘립니다.
            "stop_atr_multiple": round(lerp(5.0, 10.0, r), 2),
            "stop_ceiling_pct": round(lerp(0.12, 0.30, r), 3),
            "trailing_atr_multiple": round(lerp(3.5, 6.0, r), 2),

            # ── 주기 ──────────────────────────────────────────────────
            # 라벨(부호)과 임계값을 일치시킨다. "스캘퍼"로 표시해 놓고 일봉을
            # 주면 사용자는 둘 중 무엇을 믿어야 할지 알 수 없다.
            "timeframe": "5m" if h < -0.5 else "1h" if h < 0 else "1d",
            "hold_bars": int(round(lerp(6, 40, h))),
            "warmup_bars": int(round(lerp(200, 320, h))),

            # ── 알파 구성 ─────────────────────────────────────────────
            "prefer_trend": h > 0,
            "use_desk": e > 0.25,
            "desk_min_conviction": round(lerp(0.75, 0.52, e), 3),
            "desk_cadence_bars": int(round(lerp(8, 1, e))),
            "desk_max_symbols": int(round(lerp(1, 4, e))),

            # ── 개입 ──────────────────────────────────────────────────
            "notify_events": (
                ["order_filled", "trade_closed", "protection", "order_rejected",
                 "error", "state", "deliberation"] if c > 0.25
                else ["trade_closed", "protection", "error"] if c > -0.5
                else ["error"]
            ),
            "start_paused": c > 0.75,
            "pin_manual_buys": c > -0.5,

            # ── 하루 한도 ─────────────────────────────────────────────
            # 0 이 되는 일은 없습니다. q8 의 답이 사실상 이 값을 정합니다.
            "max_daily_loss_pct": round(lerp(0.005, 0.05, r), 4),
            "max_daily_orders": int(round(lerp(10, 60, -h))),
        }

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "description": self.description,
            "completed": self.completed,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "normalized": {a: round(self.normalized(a), 3) for a in AXIS_META},
            "overrides": self.overrides,
            "axes": self.axis_summary(),
            "settings": self.settings(),
            "updated_at": self.updated_at,
            "answers": self.answers,
        }


def score_answers(answers: dict[str, str]) -> InvestorProfile:
    """{question_id: option_id} → 프로필."""
    scores = {"R": 0.0, "H": 0.0, "E": 0.0, "C": 0.0}
    accepted: dict[str, str] = {}
    for qid, oid in answers.items():
        question = QUESTIONS_BY_ID.get(qid)
        if question is None:
            log.warning("알 수 없는 문항 무시: %s", qid)
            continue
        option = next((o for o in question.options if o.id == oid), None)
        if option is None:
            log.warning("알 수 없는 보기 무시: %s.%s", qid, oid)
            continue
        accepted[qid] = oid
        for axis, delta in option.weights.items():
            scores[axis] += delta
    return InvestorProfile(scores=scores, answers=accepted,
                           updated_at=datetime.now(UTC).isoformat())


def questionnaire() -> list[dict]:
    return [q.to_dict() for q in QUESTIONS]


class ProfileStore:
    """프로필을 JSON 한 파일로 보관. 비밀값이 아니므로 .env 와 분리합니다."""

    def __init__(self, path: str | Path = "investor_profile.json"):
        self.path = Path(path)

    def load(self) -> InvestorProfile:
        if not self.path.exists():
            return InvestorProfile()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("프로필을 읽지 못했습니다 (%s) — 기본값으로 시작합니다", exc)
            return InvestorProfile()
        return InvestorProfile(
            scores={**{"R": 0.0, "H": 0.0, "E": 0.0, "C": 0.0},
                    **(raw.get("scores") or {})},
            answers=raw.get("answers") or {},
            overrides=raw.get("overrides") or {},
            updated_at=raw.get("updated_at", ""),
        )

    def save(self, profile: InvestorProfile) -> None:
        profile.updated_at = datetime.now(UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"scores": profile.scores, "answers": profile.answers,
                        "overrides": profile.overrides,
                        "updated_at": profile.updated_at,
                        "code": profile.code, "name": profile.name},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        log.info("투자 성향 저장: %s (%s)", profile.name, profile.code)


def apply_profile(config, profile: InvestorProfile):
    """프로필을 설정에 반영한 **복사본**을 돌려준다.

    설정 파일에 명시된 값이 언제나 우선입니다. 프로필은 사용자가 직접 정하지 않은
    부분만 채웁니다 — 힘들여 고른 값을 성향 검사가 조용히 덮어쓰면, 그건 도움이
    아니라 사고입니다.
    """
    from quant.config.schema import ModelSpec

    s = profile.settings()
    cfg = config.model_copy(deep=True)
    touched: list[str] = []

    def note(what: str) -> None:
        touched.append(what)

    # 사이징 — 기본값과 같을 때만 덮어쓴다
    defaults = type(config.portfolio)()
    if cfg.portfolio.max_position_weight == defaults.max_position_weight:
        cfg.portfolio.max_position_weight = s["max_position_weight"]
        note(f"종목당 최대 비중 {s['max_position_weight']:.0%}")
    if cfg.portfolio.max_gross_leverage == defaults.max_gross_leverage:
        cfg.portfolio.max_gross_leverage = s["max_gross_leverage"]
        note(f"총 노출 {s['max_gross_leverage']:.0%}")
    if cfg.portfolio.cash_reserve_pct == defaults.cash_reserve_pct:
        cfg.portfolio.cash_reserve_pct = s["cash_reserve_pct"]
        note(f"현금 보유 {s['cash_reserve_pct']:.0%}")

    if cfg.portfolio.model.type == "vol_target":
        params = dict(cfg.portfolio.model.params)
        params.setdefault("target_annual_vol", s["target_annual_vol"])
        cfg.portfolio.model = ModelSpec(type="vol_target", params=params)
        note(f"목표 변동성 {params['target_annual_vol']:.0%}")

    # 하루 손실 한도 — 설정에 없을 때만
    if not cfg.limits.max_daily_loss_pct and not cfg.limits.max_daily_loss:
        cfg.limits.max_daily_loss_pct = s["max_daily_loss_pct"]
        note(f"하루 손실 한도 {s['max_daily_loss_pct']:.2%}")
    if not cfg.limits.max_daily_orders:
        cfg.limits.max_daily_orders = s["max_daily_orders"]
        note(f"하루 주문 {s['max_daily_orders']}건")

    # 리스크 모델 — 손절이 아예 없으면 성향에 맞는 것을 넣어 준다
    kinds = {m.type for m in cfg.risk.models}
    if "max_dd_per_security" not in kinds:
        cfg.risk.models.append(ModelSpec(
            type="max_dd_per_security",
            params={"atr_multiple": s["stop_atr_multiple"],
                    "max_drawdown_pct": s["stop_ceiling_pct"], "lock_bars": 4}))
        note(f"손절 {s['stop_atr_multiple']:.0f} ATR (상한 {s['stop_ceiling_pct']:.0%})")
    if "trailing_stop" not in kinds:
        cfg.risk.models.append(ModelSpec(
            type="trailing_stop",
            params={"atr_multiple": s["trailing_atr_multiple"],
                    "activate_at_pct": 0.05}))
        note(f"트레일링 {s['trailing_atr_multiple']:.1f} ATR")
    if "max_positions" not in kinds:
        cfg.risk.models.append(ModelSpec(
            type="max_positions", params={"max_positions": s["max_positions"]}))
        note(f"최대 {s['max_positions']}종목")

    # 알림 강도
    if cfg.notify.telegram_bot_token:
        cfg.notify.on_events = s["notify_events"]
        note(f"알림 {len(s['notify_events'])}종")

    return cfg, touched
