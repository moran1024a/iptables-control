<script lang="ts">
  // Latency over time for the probe protocols of one segment.
  // The data layer is a canvas that is repainted only when data, size or theme change; the cursor,
  // the highlighted points and the tooltip are DOM overlays, so hovering never repaints the curves.
  import { dateTime } from '$lib/format';
  import type { LinkSeries, Point } from '$lib/types';
  import { middleOf, ms, niceScale, pathSwitches, pct, plotSeries, pointAt, PROTOCOLS, stateLabel, stops, timeTicks, type Protocol } from './model';

  type Props = {
    series: LinkSeries[];
    now: number;
    /** Seconds shown, ending at `now`. */
    range: number;
    /** Protocol to emphasise; the others are dimmed. */
    focus?: Protocol | null;
    label?: string;
  };
  let { series, now, range, focus = null, label = '' }: Props = $props();

  const LEFT = 44;
  const RIGHT = 12;
  const TOP = 26;
  const ROW = 9;

  let host = $state<HTMLDivElement>();
  let canvas = $state<HTMLCanvasElement>();
  let width = $state(0);
  let themeTick = $state(0);
  /** Pointer cursors remember the pixel, keyboard cursors the time, so a poll moves neither under the user. */
  let cursor = $state<{ kind: 'pointer'; x: number } | { kind: 'key'; time: number } | null>(null);

  const height = $derived(width < 480 ? 210 : 250);
  const from = $derived(now - range);
  const plots = $derived(series.map((item) => plotSeries(item, from, now)));
  const switches = $derived(pathSwitches(series, from, now));
  const keyStops = $derived(stops(series, from, now, (series[0]?.step ?? 10) / 2));
  const scale = $derived(niceScale(Math.max(0, ...plots.map((plot) => plot.max))));
  const bottom = $derived(series.length * ROW + 8 + 22);
  const plotWidth = $derived(Math.max(1, width - LEFT - RIGHT));
  const plotHeight = $derived(Math.max(1, height - TOP - bottom));
  const x = (time: number) => LEFT + ((time - from) / range) * plotWidth;
  const y = (value: number) => TOP + plotHeight * (1 - value / scale.max);

  // The theme is an attribute on <html>; re-read the colour tokens whenever it flips.
  $effect(() => {
    const observer = new MutationObserver(() => themeTick++);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    return () => observer.disconnect();
  });

  $effect(() => {
    // Everything read here is a reason to repaint.
    const job = { el: canvas, host, width, height, plots, switches, scale, focus, from, now, range, bottom, themeTick };
    if (!job.el || !job.host || width < 40) return;
    const frame = requestAnimationFrame(() => draw(job.el!, job.host!));
    return () => cancelAnimationFrame(frame);
  });

  function draw(el: HTMLCanvasElement, root: HTMLElement) {
    const ratio = window.devicePixelRatio || 1;
    const w = Math.round(width * ratio);
    const h = Math.round(height * ratio);
    if (el.width !== w || el.height !== h) {
      el.width = w;
      el.height = h;
    }
    const ctx = el.getContext('2d');
    if (!ctx) return;
    const css = getComputedStyle(root);
    const token = (name: string) => css.getPropertyValue(name).trim();
    const border = token('--border');
    const muted = token('--fg-muted');
    const subtle = token('--fg-subtle');
    const font = (size: number) => `${size}px ${css.fontFamily}`;
    const right = LEFT + plotWidth;
    const base = TOP + plotHeight;
    const crisp = (n: number) => Math.round(n) + 0.5;

    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.lineJoin = 'round';
    ctx.lineCap = 'butt';
    ctx.textBaseline = 'middle';

    // Latency axis.
    ctx.font = font(11);
    ctx.lineWidth = 1;
    ctx.textAlign = 'right';
    for (let value = 0; value <= scale.max + scale.step / 2; value += scale.step) {
      const py = crisp(y(value));
      ctx.strokeStyle = border;
      ctx.globalAlpha = value === 0 ? 1 : 0.7;
      ctx.beginPath();
      ctx.moveTo(LEFT, py);
      ctx.lineTo(right, py);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = muted;
      ctx.fillText(String(Number(value.toFixed(2))), LEFT - 8, py);
    }
    ctx.fillStyle = subtle;
    ctx.fillText('ms', LEFT - 8, TOP - 14);

    // Time axis.
    const labelY = height - 9;
    let lastEnd = -Infinity;
    ctx.textAlign = 'center';
    for (const tick of timeTicks(from, now, plotWidth)) {
      const px = x(tick.time);
      const half = ctx.measureText(tick.label).width / 2;
      if (px - half < lastEnd + 10 || px + half > width - 2 || px - half < 2) continue;
      lastEnd = px + half;
      ctx.strokeStyle = border;
      ctx.beginPath();
      ctx.moveTo(crisp(px), base);
      ctx.lineTo(crisp(px), base + 4);
      ctx.stroke();
      ctx.fillStyle = muted;
      ctx.fillText(tick.label, px, labelY);
    }

    // Path switches: a dashed line across the plot, labelled where there is room.
    ctx.font = font(10.5);
    ctx.textAlign = 'left';
    let labelEnd = LEFT;
    switches.forEach((change, index) => {
      const px = crisp(x(change.time));
      ctx.strokeStyle = subtle;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(px, TOP - 6);
      ctx.lineTo(px, base);
      ctx.stroke();
      ctx.setLineDash([]);
      const limit = index + 1 < switches.length ? x(switches[index + 1].time) - 8 : right;
      const text = [`路径切换 · ${change.label}`, '路径切换', '切换'].find((option) => px + 4 + ctx.measureText(option).width <= limit);
      if (text && px + 4 >= labelEnd) {
        ctx.fillStyle = muted;
        ctx.fillText(text, px + 4, TOP - 13);
        labelEnd = px + 4 + ctx.measureText(text).width + 6;
      }
    });

    // Marker strip under the plot: one row per protocol.
    ctx.font = font(10.5);
    ctx.textAlign = 'right';
    ctx.fillStyle = subtle;
    if (plots.length) ctx.fillText('超时', LEFT - 8, base + 8 + ((plots.length - 1) * ROW) / 2 + 1);

    const step = series[0]?.step || 10;
    const pitch = (step / range) * plotWidth;
    plots.forEach((plot, row) => {
      const colour = token(PROTOCOLS[plot.protocol].token);
      const alpha = focus && focus !== plot.protocol ? 0.16 : 1;
      const rowY = base + 9 + row * ROW;
      ctx.strokeStyle = colour;
      ctx.fillStyle = colour;

      // Bucket peaks as faint whiskers above the mean.
      ctx.globalAlpha = alpha * 0.3;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i < plot.dots.length; i += 3) {
        if (plot.dots[i + 2] === plot.dots[i + 1]) continue;
        const px = x(plot.dots[i]);
        ctx.moveTo(px, y(plot.dots[i + 1]));
        ctx.lineTo(px, y(plot.dots[i + 2]));
      }
      ctx.stroke();

      ctx.globalAlpha = alpha;
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (const run of plot.runs) {
        ctx.moveTo(x(run[0]), y(run[1]));
        for (let i = 2; i < run.length; i += 2) ctx.lineTo(x(run[i]), y(run[i + 1]));
      }
      ctx.stroke();

      // Dots for every bucket while they have room; isolated buckets always, or they would vanish.
      const dots = pitch >= 7 ? plot.dots : plot.lone;
      const stride = pitch >= 7 ? 3 : 2;
      ctx.beginPath();
      for (let i = 0; i < dots.length; i += stride) {
        const px = x(dots[i]);
        const py = y(dots[i + 1]);
        ctx.moveTo(px + 2.5, py);
        ctx.arc(px, py, 2.5, 0, Math.PI * 2);
      }
      ctx.fill();

      // Intervals in which the probe could not run.
      ctx.globalAlpha = alpha * 0.3;
      ctx.fillStyle = subtle;
      for (let i = 0; i < plot.idle.length; i += 2) {
        const start = Math.max(LEFT, x(plot.idle[i]));
        ctx.fillRect(start, rowY - 1, Math.max(1, Math.min(right, x(plot.idle[i + 1])) - start), 2);
      }

      // Timeouts and probe errors: a cross per bucket, or a tick shaded by loss when buckets are dense.
      ctx.lineWidth = 1.5;
      for (let i = 0; i < plot.fails.length; i += 2) {
        const px = x(plot.fails[i]);
        ctx.beginPath();
        if (pitch >= 6) {
          ctx.globalAlpha = alpha;
          ctx.moveTo(px - 2.5, rowY - 2.5);
          ctx.lineTo(px + 2.5, rowY + 2.5);
          ctx.moveTo(px - 2.5, rowY + 2.5);
          ctx.lineTo(px + 2.5, rowY - 2.5);
        } else {
          ctx.globalAlpha = alpha * (0.3 + 0.7 * Math.min(1, plot.fails[i + 1] / 20));
          ctx.moveTo(px, rowY - 3);
          ctx.lineTo(px, rowY + 3);
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    });
  }

  // ----- cursor and tooltip -----
  const cursorTime = $derived.by(() => {
    if (!cursor) return null;
    if (cursor.kind === 'key') return cursor.time >= from && cursor.time <= now ? cursor.time : null;
    return Math.round(from + (Math.min(Math.max(cursor.x - LEFT, 0), plotWidth) / plotWidth) * range);
  });
  const cursorX = $derived(cursorTime == null ? 0 : cursor?.kind === 'pointer' ? Math.min(Math.max(cursor.x, LEFT), LEFT + plotWidth) : x(cursorTime));

  type Row = { protocol: Protocol; point: Point | null };
  const rows = $derived.by<Row[]>(() => {
    const time = cursorTime;
    return time == null ? [] : series.map((item) => ({ protocol: item.protocol, point: pointAt(item.points, time) }));
  });
  const switchHere = $derived.by(() => {
    const time = cursorTime;
    if (time == null) return null;
    const reach = Math.max((6 / plotWidth) * range, 1);
    const covered = (change: { time: number }) => rows.some((row) => row.point && row.point.time <= change.time && change.time < row.point.end);
    return switches.find((change) => Math.abs(change.time - time) <= reach || covered(change)) ?? null;
  });

  function detail(point: Point): string {
    const parts = [point.status === 'unknown' && point.error ? point.error : stateLabel(point.status)];
    if (point.peak != null && point.peak !== point.mean) parts.push(`峰值 ${ms(point.peak)}`);
    if (point.samples) parts.push(`超时 ${pct(point.loss)}${point.lost ? `（${point.lost}/${point.samples}）` : ''}`);
    if (point.interval) parts.push(`采样间隔 ${point.interval} 秒`);
    if (point.error && point.status !== 'unknown' && point.error !== parts[0]) parts.push(point.error);
    return parts.join(' · ');
  }

  const spoken = $derived(
    cursorTime == null
      ? '未选择时间'
      : [dateTime(cursorTime), ...rows.map((row) => `${PROTOCOLS[row.protocol].name} ${row.point ? `${ms(row.point.mean)} ${detail(row.point)}` : '未采样'}`), switchHere ? `路径切换 ${switchHere.label}` : '']
          .filter(Boolean)
          .join('，'),
  );

  const TIP = 216;
  const tipLeft = $derived.by(() => {
    const room = cursorX + 14 + TIP <= width - 4;
    return Math.max(4, room ? cursorX + 14 : cursorX - 14 - TIP);
  });

  function pointer(event: PointerEvent) {
    const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const px = event.clientX - box.left;
    const py = event.clientY - box.top;
    if (px < LEFT - 4 || px > LEFT + plotWidth + 4 || py < 0 || py > height) {
      if (cursor?.kind === 'pointer') cursor = null;
      return;
    }
    cursor = { kind: 'pointer', x: px };
  }

  function key(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      if (cursor) event.stopPropagation();
      cursor = null;
      return;
    }
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || !keyStops.length) return;
    event.preventDefault();
    const last = keyStops.length - 1;
    const time = cursorTime;
    // Position of the first stop after the cursor; the stop under the cursor, if any, sits just before it.
    let after = keyStops.findIndex((stop) => time != null && stop > time);
    if (after < 0) after = keyStops.length;
    const on = time != null && keyStops[after - 1] === time;
    let index: number;
    if (event.key === 'Home') index = 0;
    else if (event.key === 'End' || time == null) index = last;
    else if (event.key === 'ArrowLeft') index = on ? after - 2 : after - 1;
    else index = after;
    cursor = { kind: 'key', time: keyStops[Math.min(Math.max(index, 0), last)] };
  }
</script>

<!-- A slider is the closest native pattern: one value (the inspected time) moved with the arrow keys. -->
<div
  bind:this={host}
  bind:clientWidth={width}
  class="relative w-full touch-pan-y rounded-md outline-offset-2 select-none"
  style:height="{height}px"
  role="slider"
  tabindex="0"
  aria-label="{label ? `${label} ` : ''}网络延迟趋势，使用左右方向键查看数据"
  aria-orientation="horizontal"
  aria-valuemin={from}
  aria-valuemax={now}
  aria-valuenow={cursorTime ?? now}
  aria-valuetext={spoken}
  onpointermove={pointer}
  onpointerdown={pointer}
  onpointerleave={(event) => {
    if (event.pointerType !== 'touch' && cursor?.kind === 'pointer') cursor = null;
  }}
  onblur={() => (cursor = null)}
  onkeydown={key}
>
  <canvas bind:this={canvas} class="absolute inset-0 size-full" aria-hidden="true"></canvas>

  {#if cursorTime != null}
    <div class="pointer-events-none absolute w-px bg-fg-subtle" style:left="{cursorX}px" style:top="{TOP}px" style:height="{plotHeight}px"></div>
    {#each rows as row (row.protocol)}
      {#if row.point?.mean != null && (!focus || focus === row.protocol)}
        <div
          class="pointer-events-none absolute size-2.5 -translate-1/2 rounded-full ring-2 ring-surface {PROTOCOLS[row.protocol].swatch}"
          style:left="{x(middleOf(row.point))}px"
          style:top="{y(row.point.mean)}px"
        ></div>
      {/if}
    {/each}
    <div
      class="pointer-events-none absolute z-10 rounded-lg border bg-surface px-3 py-2 text-xs shadow-md"
      style:left="{tipLeft}px"
      style:top="{TOP + 4}px"
      style:width="{Math.min(TIP, width - 8)}px"
      aria-hidden="true"
    >
      <p class="tabular font-medium text-fg-muted">{dateTime(cursorTime)}</p>
      {#if switchHere}<p class="mt-1 text-fg-muted">┆ 路径切换{switchHere.label ? ` · ${switchHere.label}` : ''}</p>{/if}
      {#each rows as row (row.protocol)}
        <div class="mt-1.5 flex items-baseline justify-between gap-3">
          <span class="inline-flex items-center gap-1.5 text-fg-muted">
            <span class="h-[3px] w-3 rounded-full {PROTOCOLS[row.protocol].swatch}"></span>{PROTOCOLS[row.protocol].name}
          </span>
          <span class="tabular text-[0.8125rem] font-semibold">{ms(row.point?.mean)}</span>
        </div>
        <p class="leading-snug text-fg-subtle">{row.point ? detail(row.point) : '未采样'}</p>
      {/each}
    </div>
  {/if}
</div>
