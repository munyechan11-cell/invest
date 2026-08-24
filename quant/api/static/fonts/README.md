# 픽셀 한글 폰트 (optional)

대시보드는 한글도 픽셀 폰트로 렌더링하도록 준비되어 있습니다. 다만 픽셀 한글
폰트는 Google Fonts에 없어서 저장소에 포함하지 않았습니다 (라이선스·용량).

원하시면 아래 파일 중 하나를 이 폴더에 넣고, `index.html` 의 `<style>` 첫머리에
아래 두 줄을 되살리면 적용됩니다. 선언을 항상 켜 두지 않는 이유는, 파일이
없을 때 브라우저가 매 로드마다 없는 파일을 요청하고 콘솔에 404 를 남기기
때문입니다 — 얻는 것 없이 실패만 기록합니다.

```css
@font-face{font-family:"PixelKR";src:url("/static/fonts/galmuri11.woff2") format("woff2");font-display:swap}
@font-face{font-family:"PixelKR2";src:url("/static/fonts/neodgm.woff2") format("woff2");font-display:swap}
```

폰트 스택(`--px`, `--kr`)에는 `PixelKR` 이름이 그대로 남아 있으므로, 위 두 줄만
되살리면 나머지는 손댈 것이 없습니다.

    galmuri11.woff2     ← 권장 (Galmuri, OFL)
    neodgm.woff2        ← 대안 (둥근모꼴, OFL)

Galmuri: https://galmuri.quiple.dev  ·  둥근모꼴: https://neodgm.dalgona.dev

라틴 문자와 숫자는 Google Fonts의 Silkscreen을 사용하므로 이 폴더가 비어 있어도
가격·티커·수치는 픽셀 폰트로 보입니다.
