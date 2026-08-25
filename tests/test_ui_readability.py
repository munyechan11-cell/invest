"""The redesign keeps its readable, touchable layout as the CSS evolves.

These checks deliberately read CSS properties instead of matching whole source
lines.  Reformatting a rule is harmless; changing the user-visible invariant is
not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE = ROOT / "quant" / "api" / "static" / "app.css"
PAGE = ROOT / "quant" / "api" / "static" / "index.html"


@dataclass(frozen=True)
class Rule:
    selectors: tuple[str, ...]
    declarations: dict[str, str]
    media: str | None = None


def _split(value: str, delimiter: str) -> list[str]:
    """Split CSS outside strings and parenthesised functions."""
    parts: list[str] = []
    start = depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == delimiter and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _blocks(source: str):
    """Yield balanced ``header { body }`` blocks from one nesting level."""
    cursor = 0
    while True:
        opening = source.find("{", cursor)
        if opening < 0:
            return
        header = source[cursor:opening].strip()
        depth, quote, escaped = 1, None, False
        index = opening + 1
        while index < len(source) and depth:
            char = source[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        assert depth == 0, f"닫히지 않은 CSS block: {header}"
        yield header, source[opening + 1:index - 1]
        cursor = index


def _declarations(body: str) -> dict[str, str]:
    declarations: dict[str, str] = {}
    for chunk in _split(body, ";"):
        if ":" not in chunk:
            continue
        name, value = chunk.split(":", 1)
        declarations[name.strip().lower()] = value.strip()
    return declarations


def _normalise_selector(selector: str) -> str:
    selector = re.sub(r"\s+", " ", selector.strip())
    return re.sub(r"\s*([>+~])\s*", r"\1", selector)


def _parse_rules(source: str, media: str | None = None) -> list[Rule]:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    parsed: list[Rule] = []
    for header, body in _blocks(source):
        lowered = header.casefold()
        if lowered.startswith("@media"):
            parsed.extend(_parse_rules(body, header[len("@media"):].strip()))
        elif header.startswith("@"):
            continue
        else:
            selectors = tuple(
                _normalise_selector(selector)
                for selector in _split(header, ",")
                if selector.strip()
            )
            parsed.append(Rule(selectors, _declarations(body), media))
    return parsed


CSS = STYLE.read_text(encoding="utf-8")
RULES = _parse_rules(CSS)


def _media_applies(media: str | None, width: int | None) -> bool:
    if media is None:
        return True
    if width is None:
        return False
    limits = re.findall(r"(min|max)-width\s*:\s*(\d+(?:\.\d+)?)px", media)
    if not limits:  # e.g. prefers-reduced-motion is not part of this viewport
        return False
    return all(width >= float(value) if kind == "min" else width <= float(value)
               for kind, value in limits)


def _style(selector: str, *, width: int | None = None) -> dict[str, str]:
    """Cascade declarations for an exact selector in source order."""
    wanted = _normalise_selector(selector)
    style: dict[str, str] = {}
    for rule in RULES:
        if wanted in rule.selectors and _media_applies(rule.media, width):
            for name, value in rule.declarations.items():
                # A later shorthand resets earlier axis-specific declarations.
                # Keeping both would report an overflow that the browser no
                # longer applies.
                if name == "overflow":
                    style.pop("overflow-x", None)
                    style.pop("overflow-y", None)
                style[name] = value
    return style


def _last_compound(selector: str) -> str:
    """Return the last simple compound for our small safety-target allowlist."""
    return re.split(r"[\s>+~]+", selector.strip())[-1]


def _selector_targets(selector: str, target: str) -> bool:
    """Whether a selector can style one of the explicitly tested UI targets.

    This is deliberately not a general selector matcher.  It only recognises
    the class/id ancestry and final element used by the safety invariants below.
    That is enough to catch a more-specific ``body ...`` override without
    growing a second CSS engine inside the test suite.
    """
    target_classes = set(re.findall(r"\.([\w-]+)", target))
    target_ids = set(re.findall(r"#([\w-]+)", target))
    if not target_classes.issubset(set(re.findall(r"\.([\w-]+)", selector))):
        return False
    if not target_ids.issubset(set(re.findall(r"#([\w-]+)", selector))):
        return False

    target_final = _last_compound(target)
    selector_final = _last_compound(selector)
    if target_final.startswith("."):
        return target_final[1:] in re.findall(r"\.([\w-]+)", selector_final)
    if target_final.startswith("#"):
        return target_final[1:] in re.findall(r"#([\w-]+)", selector_final)
    return bool(re.match(rf"^{re.escape(target_final)}(?:[^\w-]|$)", selector_final))


def _specificity(selector: str) -> tuple[int, int, int]:
    """Compute specificity for the simple selectors used by safety targets."""
    without_where = re.sub(r":where\([^)]*\)", "", selector)
    ids = len(re.findall(r"#[\w-]+", without_where))
    classes = len(re.findall(r"\.[\w-]+", without_where))
    attributes = len(re.findall(r"\[[^]]+\]", without_where))
    pseudo_classes = len(re.findall(r":(?!:)[\w-]+(?:\([^)]*\))?", without_where))
    stripped = re.sub(
        r"#[\w-]+|\.[\w-]+|\[[^]]+\]|::?[\w-]+(?:\([^)]*\))?",
        " ",
        without_where,
    )
    elements = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", stripped))
    return ids, classes + attributes + pseudo_classes, elements


def _css_value(value: str) -> tuple[str, bool]:
    important = bool(re.search(r"!\s*important\s*$", value, re.I))
    return re.sub(r"\s*!\s*important\s*$", "", value, flags=re.I).strip(), important


def _possible_on_account(selector: str) -> bool:
    page_values = re.findall(
        r"\[\s*data-page\s*=\s*['\"]?([^'\"\]\s]+)", selector, re.I)
    return all(value.casefold() == "account" for value in page_values)


def _safety_value(
    target: str,
    property_name: str,
    *,
    width: int | None = None,
    account_page: bool = False,
    rules: list[Rule] | None = None,
) -> str:
    """Cascade one property for a known target, including specific overrides."""
    winner: tuple[tuple[int, int, int, int, int], str] | None = None
    for source_order, rule in enumerate(RULES if rules is None else rules):
        if not _media_applies(rule.media, width) or property_name not in rule.declarations:
            continue
        value, important = _css_value(rule.declarations[property_name])
        for selector in rule.selectors:
            if not _selector_targets(selector, target):
                continue
            if account_page and not _possible_on_account(selector):
                continue
            specificity = _specificity(selector)
            priority = (int(important), *specificity, source_order)
            if winner is None or priority >= winner[0]:
                winner = priority, value
    return winner[1] if winner else ""


def _token(name: str) -> str:
    value = _style(":root").get(f"--{name}")
    assert value, f"--{name} 색 토큰이 없습니다"
    return value


def _rgb(value: str, seen: set[str] | None = None) -> tuple[float, float, float]:
    value = value.strip()
    reference = re.fullmatch(r"var\(\s*--([\w-]+)\s*\)", value)
    if reference:
        seen = seen or set()
        name = reference.group(1)
        assert name not in seen, f"순환 CSS token: --{name}"
        return _rgb(_token(name), seen | {name})

    match = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value)
    assert match, f"대비를 계산할 수 없는 색 표현입니다: {value}"
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return tuple(int(digits[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _luminance(value: str) -> float:
    channels = [channel / 12.92 if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4
                for channel in _rgb(value)]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str) -> float:
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _assert_contrast(foreground: str, background: str, minimum: float) -> None:
    ratio = _contrast(_token(foreground), _token(background))
    assert ratio >= minimum, (
        f"--{foreground} / --{background} 대비 {ratio:.2f}:1; "
        f"최소 {minimum:.1f}:1 이어야 합니다")


def test_body_copy_does_not_fall_back_to_a_pixel_font():
    body_family = _style("body").get("font-family", "")
    assert "var(--kr)" in body_family.replace(" ", ""), "body가 --kr 본문 stack을 쓰지 않습니다"
    korean_stack = _token("kr").casefold()
    forbidden = {"pixelkr", "silkscreen"}
    assert not {font for font in forbidden if font in korean_stack}, (
        "장문용 --kr stack에 픽셀 폰트가 들어가면 한글 본문이 다시 흐려집니다")


def test_core_colour_tokens_meet_their_contrast_jobs():
    _assert_contrast("ink", "panel", 4.5)
    _assert_contrast("muted", "panel", 4.5)
    _assert_contrast("field", "panel2", 3.0)
    _assert_contrast("edge", "panel", 3.0)
    _assert_contrast("edge", "plate", 3.0)

    live_mode = _style("body.live .mode")
    assert live_mode.get("color", "").replace(" ", "") == "var(--bg)", (
        "실거래 mode badge의 작은 글자가 red 배경에서 4.5:1 대비를 잃습니다")
    assert _contrast(_token("bg"), _token("red")) >= 4.5

    source = STYLE.read_text(encoding="utf-8")
    pulse = re.search(r"@keyframes\s+livepulse\s*\{(.*?)\n\}", source, re.S)
    assert pulse and "opacity" not in pulse.group(1), (
        "실거래 badge 전체를 흐리게 pulse하면 animation 중 글자 대비가 무너집니다")


def test_dim_gold_is_not_low_contrast_small_text():
    ratio = _contrast(_token("gold-dim"), _token("panel2"))
    small_text_uses: list[str] = []
    large_text_uses: list[str] = []
    for rule in RULES:
        if (rule.declarations.get("color", "").replace(" ", "")
                != "var(--gold-dim)"):
            continue
        size = _pixel_size(rule.declarations.get("font-size", ""))
        weight = rule.declarations.get("font-weight", "").casefold()
        is_bold = weight == "bold" or (weight.isdigit() and int(weight) >= 700)
        destination = large_text_uses if size >= 24 or (is_bold and size >= 18.66) \
            else small_text_uses
        destination.extend(rule.selectors)

    assert ratio >= 4.5 or not small_text_uses, (
        f"--gold-dim 대비가 {ratio:.2f}:1 인데 작은 글자색으로 쓰입니다: "
        + ", ".join(small_text_uses))
    assert ratio >= 3.0 or not large_text_uses, (
        f"--gold-dim 대비가 {ratio:.2f}:1 인데 큰 글자색으로도 부족합니다: "
        + ", ".join(large_text_uses))


def _pixel_size(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)px", value.strip())
    return float(match.group(1)) if match else 0.0


def test_primary_interactions_keep_a_44px_touch_height():
    targets = {
        "상단 페이지 탭": ".pagetabs button",
        "전략 상세 펼치기": ".stmore summary",
        "재생 제어": ".playbar button",
        "성향 선택지": ".opt",
        "홈 브랜드": ".brand",
    }
    too_short = {}
    for width in (1440, 900, 760, 420, 320):
        for label, selector in targets.items():
            size = _pixel_size(_safety_value(selector, "min-height", width=width))
            if size < 44:
                too_short[f"{label}@{width}px"] = size
    assert not too_short, f"44px보다 작은 핵심 touch target: {too_short}"


def _grid_track_count(value: str) -> int:
    tracks: list[str] = []
    start = depth = 0
    for index, char in enumerate(value.strip()):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char.isspace() and depth == 0:
            if value[start:index].strip():
                tracks.append(value[start:index].strip())
            start = index + 1
    if value[start:].strip():
        tracks.append(value[start:].strip())

    count = 0
    for track in tracks:
        repeated = re.fullmatch(r"repeat\(\s*(\d+)\s*,.*\)", track)
        count += int(repeated.group(1)) if repeated else 1
    return count


def _percentage(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)%", value.strip())
    return float(match.group(1)) if match else 0.0


def test_mobile_header_is_a_two_row_grid_without_horizontal_scroll():
    for width in (900, 760, 420, 320):
        header = _style(".top-in", width=width)
        assert header.get("display") == "grid", f"{width}px header가 grid가 아닙니다"
        rows = header.get("grid-template-rows", "")
        assert _grid_track_count(rows) == 2, (
            f"{width}px header는 2행이어야 합니다: {rows or '미정의'}")
        overflow_x = header.get("overflow-x", header.get("overflow", "visible"))
        assert overflow_x == "visible", (
            f"{width}px header가 내용을 숨기거나 가로 scroll을 만듭니다: "
            f"overflow-x={overflow_x}")
        assert "minmax(0,1fr)" in header.get("grid-template-columns", "").replace(" ", ""), (
            f"{width}px header column이 viewport 안으로 줄어들 수 없습니다")

        children = _style(".top-in>*", width=width)
        assert children.get("min-width") == "0", (
            f"{width}px header 자식이 grid column 밖으로 넘칠 수 있습니다")
        tabs_slot = _style(".top-in>.pagetabs", width=width)
        tab_button = _style(".pagetabs button", width=width)
        tabs_display = _safety_value(".pagetabs", "display", width=width)
        assert tabs_display != "none", (
            f"{width}px에서 핵심 page navigation이 숨겨집니다")
        assert tabs_slot.get("grid-column", "").replace(" ", "") == "1/-1", (
            f"{width}px page tabs가 header 전체 폭을 쓰지 않습니다")
        assert tabs_slot.get("width") == "100%", (
            f"{width}px page tabs 폭이 viewport grid보다 커질 수 있습니다")
        assert 0 < _percentage(tab_button.get("width", "")) <= 34, (
            f"{width}px page tab 하나가 navigation row에 맞지 않습니다")
        tools_height = _safety_value(
            "#headToolsToggle", "min-height", width=width)
        assert _pixel_size(tools_height) >= 44, (
            f"{width}px 보조 도구 열기 버튼이 44px보다 작습니다")
        mode = _style(".top-in>.mode", width=width)
        assert _pixel_size(mode.get("font-size", "")) >= 12, (
            f"{width}px 운영 mode 글자가 12px보다 작습니다")
        assert mode.get("grid-column", "").replace(" ", "") == "5/8", (
            f"{width}px 운영 mode가 한글 상태를 표시할 3개 grid column을 갖지 않습니다")


def _single_column(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return compact in {
        "1fr", "minmax(0,1fr)", "repeat(1,1fr)",
        "repeat(1,minmax(0,1fr))",
    }


def test_ticket_fields_stack_in_one_column_at_420px():
    for width in (420, 320):
        columns = _style(".ticket .row", width=width).get("grid-template-columns", "")
        assert _single_column(columns), (
            f"{width}px 주문 입력칸이 단일열이 아닙니다: {columns or '미정의'}")


def test_page_switcher_is_navigation_not_an_incomplete_tab_widget():
    markup = PAGE.read_text(encoding="utf-8")
    navigation = re.search(
        r'<nav\b[^>]*\bid="pageTabs"[^>]*>(.*?)</nav>', markup, re.S)
    assert navigation, "주요 화면 navigation이 없습니다"
    whole = navigation.group(0)
    buttons = re.findall(r"<button\b([^>]*)>", navigation.group(1))
    assert len(buttons) == 3, "매매·내 계좌·마이페이지 이동 버튼이 모두 필요합니다"
    assert 'role="tablist"' not in whole and all('role="tab"' not in b for b in buttons), (
        "화살표 키·tabpanel 연결이 없는 page switcher를 ARIA tab으로 선언하면 안 됩니다")
    assert sum('aria-current="page"' in button for button in buttons) == 1, (
        "초기 현재 화면은 하나만 aria-current=page로 표시해야 합니다")


def _assert_show_page_updates_aria_current(markup: str) -> None:
    function = re.search(
        r"function\s+showPage\s*\([^)]*\)\s*\{(.*?)\n\}\s*\n\s*"
        r"\$\(['\"]#pageTabs['\"]\)",
        markup,
        re.S,
    )
    assert function, "showPage 함수 또는 pageTabs click binding을 찾을 수 없습니다"
    loop = re.search(
        r"\$\(['\"]#pageTabs['\"]\)\.querySelectorAll\("
        r"['\"]\[data-page\]['\"]\)\.forEach\(\s*(\w+)\s*=>\s*\{(.*?)\}\s*\)",
        function.group(1),
        re.S,
    )
    assert loop, "showPage가 모든 page navigation button을 갱신하지 않습니다"
    button = re.escape(loop.group(1))
    body = loop.group(2)
    assert re.search(
        rf"if\s*\(\s*{button}\.dataset\.page\s*===\s*page\s*\)\s*"
        rf"{button}\.setAttribute\(\s*['\"]aria-current['\"]\s*,\s*"
        r"['\"]page['\"]\s*\)",
        body,
        re.S,
    ), "showPage가 새 현재 화면에 aria-current=page를 설정하지 않습니다"
    assert re.search(
        rf"else\s+{button}\.removeAttribute\(\s*['\"]aria-current['\"]\s*\)",
        body,
        re.S,
    ), "showPage가 이전 화면 button의 aria-current를 제거하지 않습니다"


def test_show_page_updates_aria_current_for_every_navigation_button():
    _assert_show_page_updates_aria_current(PAGE.read_text(encoding="utf-8"))


def test_room_grid_does_not_stretch_short_rooms_into_empty_stages():
    rooms = _style("#rooms")
    room = _style(".room")
    assert rooms.get("align-items") == "start", (
        "2x2 room grid가 같은 행의 짧은 방을 긴 빈 무대로 늘립니다")
    assert room.get("height") == "auto", (
        "room의 100% 높이가 grid stretch 방지를 무효화합니다")


def test_duplicate_manual_order_form_stays_collapsed_as_an_auxiliary_path():
    markup = PAGE.read_text(encoding="utf-8")
    details = re.search(
        r'<details\b([^>]*)\bclass="manual-orders"([^>]*)>(.*?)</details>',
        markup,
        re.S,
    )
    assert details, "전략 밖 종목용 보조 주문 영역이 없습니다"
    opening_attributes = details.group(1) + details.group(2)
    assert not re.search(r"(?:^|\s)open(?:\s|=|$)", opening_attributes), (
        "보조 주문이 기본으로 펼쳐지면 오른쪽 기본 ticket과 중복됩니다")
    body = details.group(3)
    assert 'id="mBuy"' in body and 'id="mSell"' in body, (
        "기존 보조 주문 runtime contract가 collapsed 영역 안에 있어야 합니다")
    assert 'id="cBuy"' not in body and 'id="cSell"' not in body, (
        "오른쪽 기본 주문 ticket은 보조 영역과 분리되어야 합니다")

    for width in (1440, 900, 420, 320):
        assert _closed_manual_content_wins(width=width), (
            f"{width}px에서 닫힌 보조 주문의 내용이 author display 규칙 때문에 보입니다")


def _closed_manual_content_wins(
    *, width: int, rules: list[Rule] | None = None,
) -> bool:
    active_rules = RULES if rules is None else rules
    closed_selector = ".manual-orders:not([open])>:not(summary)"
    hidden_priorities: list[tuple[int, int, int, int, int]] = []
    reveal_priorities: list[tuple[int, int, int, int, int]] = []
    for source_order, rule in enumerate(active_rules):
        if not _media_applies(rule.media, width) or "display" not in rule.declarations:
            continue
        value, important = _css_value(rule.declarations["display"])
        for selector in rule.selectors:
            priority = (int(important), *_specificity(selector), source_order)
            if selector == closed_selector and value == "none":
                hidden_priorities.append(priority)
            elif value != "none" and any(
                _selector_targets(selector, target)
                for target in (".manual", ".actions")
            ):
                reveal_priorities.append(priority)
    return bool(hidden_priorities) and (
        not reveal_priorities or max(hidden_priorities) > max(reveal_priorities)
    )


class _AccountClasses(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.account_depth: int | None = None
        self.classes: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id") == "pageAccount":
            self.account_depth = self.depth
        if self.account_depth is not None:
            self.classes.update(attributes.get("class", "").split())
        if tag not in self.VOID:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.depth -= 1

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        self.depth -= 1
        if self.account_depth is not None and self.depth == self.account_depth:
            self.account_depth = None


def test_account_page_uses_the_full_deck_and_has_a_summary_grid():
    for width in (1440, 900, 760, 420, 320):
        deck_columns = _safety_value(
            ".deck",
            "grid-template-columns",
            width=width,
            account_page=True,
        )
        assert _single_column(deck_columns), (
            f"{width}px 계좌 화면이 2열 deck으로 돌아갔습니다: "
            f"{deck_columns or '미정의'}")

    parser = _AccountClasses()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    candidates = [selector for class_name, selector in (
        ("kpis", ".kpis"), ("pnlgrid", ".pnlgrid"))
        if class_name in parser.classes]
    grids = [selector for selector in candidates
             if _style(selector).get("display") == "grid"
             and "repeat(" in _style(selector).get("grid-template-columns", "")]
    assert grids, "계좌 핵심 수치를 훑어볼 responsive summary grid가 없습니다"


def test_safety_scan_catches_more_specific_mobile_mutations():
    mutated_rules = _parse_rules(
        """
        .pagetabs { display: flex; }
        .pagetabs button { min-height: 44px; }
        .deck { grid-template-columns: 1fr 380px; }
        body[data-page="account"] .deck { grid-template-columns: 1fr; }
        @media(max-width: 900px) {
          body.authed .pagetabs { display: none; }
          body.authed .pagetabs button { min-height: 28px; }
          body.authed[data-page="account"] .deck {
            grid-template-columns: 1fr 320px;
          }
        }
        """
    )
    assert _safety_value(
        ".pagetabs", "display", width=420, rules=mutated_rules) == "none"
    assert _safety_value(
        ".pagetabs button", "min-height", width=420, rules=mutated_rules,
    ) == "28px"
    assert not _single_column(_safety_value(
        ".deck",
        "grid-template-columns",
        width=420,
        account_page=True,
        rules=mutated_rules,
    ))

    details_mutation = _parse_rules(
        """
        .manual-orders:not([open])>:not(summary) { display: none; }
        .manual { display: grid; }
        .actions { display: flex; }
        body.authed .manual-orders > .actions { display: flex !important; }
        """
    )
    assert not _closed_manual_content_wins(width=420, rules=details_mutation)
