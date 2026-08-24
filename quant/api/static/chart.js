/* 시세 차트 — 왼쪽에서 결정한 것이 오른쪽에서 보이게.
 *
 * 이 화면의 요점은 예쁜 캔들이 아니라 세 가지입니다.
 *
 *   1. 내가 얼마에 들어갔는가.       진입가를 가로선으로 긋습니다.
 *   2. 봇이 방금 무엇을 했는가.       체결을 봉 위에 점으로 찍습니다. 자동매매
 *      중이어도 왼쪽 데스크가 심의해서 낸 주문이 여기 나타납니다.
 *   3. 지금 사려면 얼마인가.          차트를 누르면 그 가격이 지정가로 들어갑니다.
 *
 * 데스크가 "지금은 눌림목을 기다려라" 라고 말했을 때, 그 눌림목이 어디인지
 * 보면서 그 자리에 주문을 걸 수 있어야 한다는 뜻입니다.
 *
 * 서버 계약 (GET /api/candles?ticker=&timeframe=&count=):
 *   { ticker, timeframe, currency, tick_size,
 *     bars:     [{t,o,h,l,c,v}, ...]        오래된 것부터
 *     quote:    {price, change_pct, ts} | null
 *     position: {quantity, avg_price, unrealized_pct} | null
 *     orders:   [{side, price, quantity, status}]      미체결
 *     fills:    [{ts, side, price, quantity, tag}]     이 구간에 체결된 것
 *     stale:    bool }                                 장 마감/피드 지연
 */
(function () {
  'use strict';

  const CSS = getComputedStyle(document.documentElement);
  const tok = (name, fallback) => (CSS.getPropertyValue(name) || fallback).trim();
  const C = {
    up: tok('--sang', '#FF4D5C'),
    down: tok('--ha', '#4D93FF'),
    gold: tok('--gold', '#FFC64A'),
    bone: tok('--bone', '#E9E5DA'),
    dim: tok('--dim', '#9FA9C6'),
    faint: tok('--faint', '#8790B0'),
    rule: tok('--rule', '#28304E'),
    edge: tok('--edge', '#3A4570'),
    panel: tok('--panel', '#141A2C'),
    void: tok('--void', '#080B14'),
  };

  const PAD = { l: 8, r: 62, t: 10, b: 20 };
  const won = (n, cur) => (cur === 'KRW'
    ? Math.round(n).toLocaleString('ko-KR')
    : n.toLocaleString('en-US', { maximumFractionDigits: 2 }));

  class PriceChart {
    constructor(canvas) {
      this.cv = canvas;
      this.ctx = canvas.getContext('2d');
      this.data = null;
      this.hover = null;      // {x, y} 마우스/손가락 위치
      this.picked = null;     // 사용자가 고른 지정가
      this.onPick = null;     // (price) => void
      this._bind();
    }

    _bind() {
      const pos = (ev) => {
        const r = this.cv.getBoundingClientRect();
        const p = ev.touches ? ev.touches[0] : ev;
        return { x: p.clientX - r.left, y: p.clientY - r.top };
      };
      const move = (ev) => { this.hover = pos(ev); this.draw(); };
      const leave = () => { this.hover = null; this.draw(); };

      this.cv.addEventListener('mousemove', move);
      this.cv.addEventListener('mouseleave', leave);
      this.cv.addEventListener('touchmove', (ev) => { move(ev); ev.preventDefault(); },
                               { passive: false });
      this.cv.addEventListener('touchend', leave);

      const pick = (ev) => {
        if (!this.data || !this._scale) return;
        const price = this._scale.priceAt(pos(ev).y);
        this.picked = this._tick(price);
        this.draw();
        if (this.onPick) this.onPick(this.picked);
      };
      this.cv.addEventListener('click', pick);
    }

    /** 호가단위로 맞춥니다. 틱에 안 맞는 지정가는 거래소가 거절합니다. */
    _tick(price) {
      const t = (this.data && this.data.tick_size) || 0;
      return t > 0 ? Math.round(price / t) * t : price;
    }

    set(data) {
      this.data = data;
      if (this.picked == null && data && data.quote) this.picked = this._tick(data.quote.price);
      this.draw();
    }

    clearPick() { this.picked = null; this.draw(); }

    resize() {
      const r = this.cv.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.cv.width = Math.max(1, Math.round(r.width * dpr));
      this.cv.height = Math.max(1, Math.round(r.height * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.W = r.width; this.H = r.height;
      this.draw();
    }

    draw() {
      const g = this.ctx;
      if (!this.W) this.resize();
      g.clearRect(0, 0, this.W, this.H);
      g.fillStyle = C.void;
      g.fillRect(0, 0, this.W, this.H);

      const d = this.data;
      if (!d || !d.bars || d.bars.length < 2) {
        g.fillStyle = C.faint;
        g.font = '12px "IBM Plex Sans KR", system-ui, sans-serif';
        g.textAlign = 'center';
        g.fillText(d ? '봉 데이터가 없습니다' : '불러오는 중…', this.W / 2, this.H / 2);
        return;
      }

      const bars = d.bars;
      const x0 = PAD.l, x1 = this.W - PAD.r;
      const y0 = PAD.t, y1 = this.H - PAD.b;

      // 값 범위 — 진입가와 미체결 주문도 화면 안에 들어와야 의미가 있습니다.
      let lo = Infinity, hi = -Infinity;
      for (const b of bars) { if (b.l < lo) lo = b.l; if (b.h > hi) hi = b.h; }
      const extras = [];
      if (d.position && d.position.avg_price > 0) extras.push(d.position.avg_price);
      (d.orders || []).forEach((o) => o.price > 0 && extras.push(o.price));
      for (const v of extras) { if (v < lo) lo = v; if (v > hi) hi = v; }
      const span = (hi - lo) || Math.max(hi * 0.01, 1);
      lo -= span * 0.06; hi += span * 0.06;

      const yOf = (p) => y1 - ((p - lo) / (hi - lo)) * (y1 - y0);
      const priceAt = (y) => lo + ((y1 - y) / (y1 - y0)) * (hi - lo);
      const step = (x1 - x0) / bars.length;
      const xOf = (i) => x0 + step * (i + 0.5);
      this._scale = { yOf, priceAt, xOf, step, x0, x1, y0, y1 };

      // 가로 눈금 — 5칸이면 읽히고 그 이상은 격자만 시끄러워집니다.
      g.font = '10px "IBM Plex Mono", ui-monospace, monospace';
      g.textBaseline = 'middle';
      for (let k = 0; k <= 4; k++) {
        const p = lo + ((hi - lo) * k) / 4;
        const y = Math.round(yOf(p)) + 0.5;
        g.strokeStyle = C.rule; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
        g.fillStyle = C.faint; g.textAlign = 'left';
        g.fillText(won(p, d.currency), x1 + 5, y);
      }

      // 봉. 한국 색 논리 — 오르면 빨강.
      const bw = Math.max(1, Math.min(9, step * 0.62));
      bars.forEach((b, i) => {
        const up = b.c >= b.o;
        const col = up ? C.up : C.down;
        const cx = Math.round(xOf(i)) + 0.5;
        g.strokeStyle = col; g.lineWidth = 1;
        g.beginPath(); g.moveTo(cx, yOf(b.h)); g.lineTo(cx, yOf(b.l)); g.stroke();
        const top = yOf(Math.max(b.o, b.c)), bot = yOf(Math.min(b.o, b.c));
        g.fillStyle = col;
        g.fillRect(Math.round(cx - bw / 2), Math.round(top),
                   Math.max(1, Math.round(bw)), Math.max(1, Math.round(bot - top)));
      });

      // 내 진입가 — 이 화면의 첫 번째 질문에 대한 답.
      if (d.position && d.position.avg_price > 0 && d.position.quantity !== 0) {
        const y = Math.round(yOf(d.position.avg_price)) + 0.5;
        g.strokeStyle = C.gold; g.lineWidth = 1;
        g.setLineDash([4, 3]);
        g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
        g.setLineDash([]);
        const pct = d.position.unrealized_pct;
        const label = `진입 ${won(d.position.avg_price, d.currency)}`
          + (pct == null ? '' : `  ${pct >= 0 ? '+' : ''}${(pct * 100).toFixed(2)}%`);
        this._tag(g, x0 + 4, y, label, C.gold, 'left');
      }

      // 미체결 주문 — 걸어둔 자리가 어디인지.
      (d.orders || []).forEach((o) => {
        if (!(o.price > 0)) return;
        const y = Math.round(yOf(o.price)) + 0.5;
        const col = o.side === 'buy' ? C.up : C.down;
        g.strokeStyle = col; g.lineWidth = 1; g.setLineDash([2, 4]);
        g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
        g.setLineDash([]);
      });

      // 체결 — 봇이 방금 한 일. 자동매매여도 여기 찍힙니다.
      const first = new Date(bars[0].t).getTime();
      const last = new Date(bars[bars.length - 1].t).getTime();
      (d.fills || []).forEach((f) => {
        const t = new Date(f.ts).getTime();
        if (!(t >= first && t <= last + 1) || !(f.price > 0)) return;
        const i = Math.round(((t - first) / Math.max(1, last - first)) * (bars.length - 1));
        const cx = xOf(Math.max(0, Math.min(bars.length - 1, i)));
        const cy = yOf(f.price);
        g.fillStyle = f.side === 'buy' ? C.up : C.down;
        g.strokeStyle = C.void; g.lineWidth = 2;
        g.beginPath(); g.arc(cx, cy, 3.5, 0, Math.PI * 2);
        g.stroke(); g.fill();
      });

      // 현재가
      if (d.quote && d.quote.price > 0) {
        const y = Math.round(yOf(d.quote.price)) + 0.5;
        g.strokeStyle = C.bone; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
        this._tag(g, x1 + 2, y, won(d.quote.price, d.currency),
                  d.stale ? C.faint : C.bone, 'left', true);
      }

      // 고른 지정가
      if (this.picked > 0) {
        const y = Math.round(yOf(this.picked)) + 0.5;
        g.strokeStyle = C.gold; g.lineWidth = 1;
        g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
      }

      // 커서
      if (this.hover && this.hover.y > y0 && this.hover.y < y1) {
        const y = Math.round(this.hover.y) + 0.5;
        g.strokeStyle = C.edge; g.lineWidth = 1; g.setLineDash([1, 3]);
        g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke();
        g.setLineDash([]);
        this._tag(g, x1 + 2, y, won(this._tick(priceAt(this.hover.y)), d.currency),
                  C.dim, 'left');
      }
    }

    _tag(g, x, y, text, colour, align, strong) {
      g.font = (strong ? '600 ' : '') + '10px "IBM Plex Mono", ui-monospace, monospace';
      g.textAlign = align;
      const w = g.measureText(text).width + 6;
      g.fillStyle = C.panel;
      g.fillRect(align === 'left' ? x - 1 : x - w, y - 7, w, 14);
      g.strokeStyle = colour; g.lineWidth = 1;
      g.strokeRect(align === 'left' ? x - 1 : x - w, y - 7, w, 14);
      g.fillStyle = colour;
      g.fillText(text, align === 'left' ? x + 2 : x - 3, y);
    }
  }

  window.PriceChart = PriceChart;
})();
