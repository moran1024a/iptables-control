// Pure logic of the network quality page: labels, state explanations and chart geometry in data units.
import type { LinkSeries, Point, Range } from '$lib/types';

export type Protocol = LinkSeries['protocol'];
export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'idle';

/** `token` is the CSS variable the canvas reads; `swatch` the matching utility for DOM keys. */
export const PROTOCOLS: Record<Protocol, { name: string; token: string; swatch: string }> = {
  'tcp-connect-v1': { name: 'TCP', token: '--warn', swatch: 'bg-warn' },
  'udp-echo-v1': { name: 'UDP', token: '--info', swatch: 'bg-info' },
  'icmp-echo-v1': { name: 'ICMP', token: '--ok', swatch: 'bg-ok' },
};

export const RANGES: { value: Range; label: string }[] = [
  { value: 900, label: '15 分钟' },
  { value: 3600, label: '1 小时' },
  { value: 86400, label: '24 小时' },
  { value: 604800, label: '7 天' },
];

export const parseRange = (raw: string | null): Range => RANGES.find((item) => String(item.value) === raw)?.value ?? 900;

export const STATES: Record<string, { label: string; tone: Tone }> = {
  ok: { label: '成功', tone: 'ok' },
  timeout: { label: '探测超时', tone: 'warn' },
  error: { label: '探测异常', tone: 'danger' },
  offline: { label: '节点离线', tone: 'danger' },
  collecting: { label: '持续采集中', tone: 'info' },
  unknown: { label: '未知', tone: 'idle' },
  stopped: { label: '探测已停止', tone: 'idle' },
  unsupported: { label: '需要升级 Agent', tone: 'warn' },
  disabled: { label: '探测已关闭', tone: 'idle' },
  waiting: { label: '等待探测就绪', tone: 'idle' },
  missing: { label: '未采样', tone: 'idle' },
};

export const stateLabel = (state: string) => STATES[state]?.label ?? state;

/** One decimal with unit; "—" when there is no value. */
export const ms = (value: number | null | undefined) => (value == null ? '—' : value.toFixed(1) + ' ms');
export const pct = (value: number | null | undefined) => (value == null ? '—' : value.toFixed(1) + '%');

/** The latest raw sample, only while it still describes the present (≤ 25 s old, on the current path). */
export function freshLatest(series: LinkSeries, now: number) {
  const latest = series.latest;
  if (!latest || now - latest.slot > 25) return null;
  if (series.path_key != null && latest.path_key !== series.path_key) return null;
  return latest;
}

/** State shown for a series: the latest sample's result while probing, otherwise the reason it is not. */
export function displayState(series: LinkSeries, now: number): string {
  const latest = freshLatest(series, now);
  return (series.state === 'collecting' || (series.state as string) === 'ok') && latest ? latest.status : series.state;
}

export interface Explanation {
  label: string;
  tone: Tone;
  /** What it means and what to do; empty while everything is normal. */
  text: string;
  /** Where the user can act: the probing node, or the link itself. */
  action?: 'node' | 'link';
  /** False when this protocol can never measure this segment (not a fault). */
  applicable: boolean;
  /** True while probes are being sent. */
  measuring: boolean;
}

const NOT_APPLICABLE: Record<string, string> = {
  '目标未提供 UDP 回显': '目标不提供 UDP 回显服务，到目标的这一段无法用 UDP 测量。',
  'ICMP 无法沿端口转发路径探测': 'ICMP 不经过端口转发，无法沿这条转发路径测量。',
  '节点间使用 UDP / ICMP 探测': '节点之间的质量用 UDP 和 ICMP 测量，不做 TCP 探测。',
};

/** Turn a series state and the server's reason note into something a person can act on. */
export function explain(series: LinkSeries, now: number): Explanation {
  const state = displayState(series, now);
  const note = series.note || '';
  const base = { label: stateLabel(state), tone: STATES[state]?.tone ?? 'idle', applicable: true, measuring: false } as const;
  const reason = note ? `（${note}）` : '';
  switch (state) {
    case 'ok':
      return { ...base, text: '', measuring: true };
    case 'timeout':
      return { ...base, text: '最近一次探测没有在时限内得到响应。偶发属正常；持续出现请检查这一段的线路或对端。', measuring: true };
    case 'collecting':
      return { ...base, text: '探测已开始，首个结果约 10 秒后出现。', measuring: true };
    case 'error': {
      const detail = note || series.latest?.error || '';
      const measuring = series.state === 'collecting';
      return {
        ...base,
        text: `${measuring ? '最近一次探测失败' : '探测无法运行'}${detail ? `：${detail}` : ''}。请检查发起端节点的探测端口是否被占用或被防火墙拦截。`,
        action: 'node',
        measuring,
      };
    }
    case 'offline':
      return { ...base, text: `发起探测的节点当前未连接${reason}。节点恢复连接后会自动继续探测，离线期间没有数据。`, action: 'node' };
    case 'unsupported':
      return { ...base, text: '发起端节点的 Agent 版本过旧，不支持质量探测。请在节点页升级该节点的 Agent，升级后自动开始。', action: 'node' };
    case 'disabled':
      return { ...base, text: '发起端节点关闭了质量探测（探测端口为 0）。在节点的“编辑 → 更多设置”里填写质量探测端口即可恢复。', action: 'node' };
    case 'waiting':
      return { ...base, text: `${note || '等待探测就绪'}：配置下发并被节点确认后自动开始，无需操作。长时间停留在此状态时，请到链路页查看同步进度。`, action: 'link' };
    case 'stopped':
      return { ...base, text: `这条路径上的探测已停止${reason}，路径重新生效后会自动恢复。`, action: 'link' };
    case 'unknown':
      if (NOT_APPLICABLE[note]) return { ...base, label: '不适用', text: NOT_APPLICABLE[note], applicable: false };
      if (note === 'direct' || note === '无可探测的中间节点')
        return { ...base, label: '无法探测', text: '这条路径没有中间节点。探测由第一个中间节点发起，入口直连目标时没有可测量的段。', applicable: false };
      if (note === '发起端不是已确认的主动节点')
        return { ...base, label: '无法探测', text: '探测只能由已连接并确认的主动节点发起；被动节点或尚未接入的节点无法发起探测。', action: 'node' };
      return { ...base, text: note || '暂时无法判断探测状态。' };
    default:
      return { ...base, text: note };
  }
}

export interface RangeStats { samples: number; lost: number; loss: number | null; mean: number | null; peak: number | null }

/** Aggregate over whatever points are loaded (the selected range), weighting bucket means by their answered probes. */
export function rangeStats(points: Point[]): RangeStats {
  let samples = 0;
  let lost = 0;
  let sum = 0;
  let weight = 0;
  let peak: number | null = null;
  for (const p of points) {
    samples += p.samples;
    lost += p.lost;
    if (p.mean != null) {
      const answered = Math.max(1, p.samples - p.lost);
      sum += p.mean * answered;
      weight += answered;
      const top = p.peak ?? p.mean;
      if (peak == null || top > peak) peak = top;
    }
  }
  return { samples, lost, loss: samples ? (lost / samples) * 100 : null, mean: weight ? sum / weight : null, peak };
}

const MEASURED = new Set(['ok', 'timeout', 'error']);
const firstOf = (p: Point) => p.first_slot ?? p.time;
const lastOf = (p: Point) => p.last_slot ?? p.time;
/** Where a bucket is drawn: the middle of the samples it really contains. */
export const middleOf = (p: Point) => (firstOf(p) + lastOf(p)) / 2;

/** Buckets that contain a timeout or a probe error. Intervals that were not measurable at all do not count. */
export const isFailure = (p: Point) => p.lost > 0 || p.status === 'timeout' || p.status === 'error' || (p.failures > 0 && MEASURED.has(p.status));

export interface SeriesPlot {
  protocol: Protocol;
  /** Poly-lines as flat [time, ms, time, ms, …]; a new array starts wherever the data is not continuous. */
  runs: number[][];
  /** Every measured bucket as flat [time, mean, peak, …]. */
  dots: number[];
  /** Buckets with no continuous neighbour, flat [time, mean, …]; always drawn as a dot so they stay visible. */
  lone: number[];
  /** Failure markers, flat [time, loss %, …]. */
  fails: number[];
  /** Intervals in which the probe could not run, flat [from, to, …]. */
  idle: number[];
  max: number;
}

/**
 * Chart geometry for one series. Buckets are only joined when the server says they are continuous and they
 * share path and resolved address; anything else ends the line, so missing data is a visible gap.
 */
export function plotSeries(series: Pick<LinkSeries, 'protocol' | 'points'>, from: number, to: number): SeriesPlot {
  const out: SeriesPlot = { protocol: series.protocol, runs: [], dots: [], lone: [], fails: [], idle: [], max: 0 };
  let previous: Point | null = null;
  let run: number[] = [];
  let count = 0;
  let loneAt: [number, number] | null = null;
  const finish = () => {
    if (!previous) return;
    const mean = previous.mean as number;
    if (!previous.gap && !previous.endpoint_change && lastOf(previous) !== middleOf(previous)) run.push(lastOf(previous), mean);
    if (run.length >= 4) out.runs.push(run);
    if (count === 1 && loneAt) out.lone.push(loneAt[0], loneAt[1]);
    run = [];
    count = 0;
    previous = null;
  };
  for (const p of series.points) {
    if (p.time < from || p.time > to) continue;
    if (isFailure(p)) out.fails.push(p.first_slot != null ? middleOf(p) : (p.time + Math.min(p.end, to)) / 2, p.loss ?? 100);
    else if (p.mean == null && p.status !== 'missing' && p.status !== 'ok') {
      // Merge adjacent unmeasurable buckets into one span.
      const n = out.idle.length;
      if (n && out.idle[n - 1] >= p.time) out.idle[n - 1] = p.end;
      else out.idle.push(p.time, p.end);
    }
    if (p.mean == null) {
      // An empty bucket between two timestamped samples can be a probe that arrived slightly late; the
      // next bucket's `connect` flag decides. Everything else ends the line here.
      if (p.status !== 'missing' || previous?.last_slot == null) finish();
      continue;
    }
    const before: Point | null = previous;
    const near = before != null && (p.connect ?? (p.first_slot == null || before.last_slot == null || p.first_slot - before.last_slot <= 20));
    const mid = middleOf(p);
    if (before && near && !before.gap && !p.gap && !before.endpoint_change && !p.endpoint_change && before.resolved_ip === p.resolved_ip && before.path_key === p.path_key) {
      run.push(mid, p.mean);
      count++;
    } else {
      finish();
      if (!p.gap && !p.endpoint_change && firstOf(p) !== mid) run.push(firstOf(p), p.mean);
      run.push(mid, p.mean);
      count = 1;
      loneAt = [mid, p.mean];
    }
    const peak = p.peak ?? p.mean;
    out.dots.push(mid, p.mean, peak);
    if (peak > out.max) out.max = peak;
    previous = p;
  }
  finish();
  return out;
}

export interface PathSwitch { time: number; label: string }

/** Moments at which the measured path changed, once per timestamp across all series. */
export function pathSwitches(list: Pick<LinkSeries, 'points'>[], from: number, to: number): PathSwitch[] {
  const found = new Map<number, string>();
  for (const series of list) {
    let key: string | null = null;
    for (const p of series.points) {
      if (p.time >= from && p.time <= to && (p.endpoint_change || (p.path_key && key && key !== p.path_key)) && !found.has(p.time)) found.set(p.time, p.path_label || '');
      if (p.path_key) key = p.path_key;
    }
  }
  return [...found].map(([time, label]) => ({ time, label })).sort((a, b) => a.time - b.time);
}

/** The point whose [time, end) covers `time` (points are sorted oldest first). */
export function pointAt(points: Point[], time: number): Point | null {
  let low = 0;
  let high = points.length - 1;
  let hit = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (points[mid].time <= time) {
      hit = mid;
      low = mid + 1;
    } else high = mid - 1;
  }
  if (hit < 0) return null;
  const p = points[hit];
  return time === p.time || time < p.end ? p : null;
}

/**
 * Times to step through with the keyboard: one per bucket that holds data. Protocols are probed a second or
 * so apart, so times closer than `merge` seconds count as one stop, placed at the latest of them where every
 * protocol's bucket has already begun.
 */
export function stops(list: Pick<LinkSeries, 'points'>[], from: number, to: number, merge = 0): number[] {
  const times = new Set<number>();
  for (const series of list) for (const p of series.points) if (p.status !== 'missing' && p.time >= from && p.time <= to) times.add(p.time);
  const out: number[] = [];
  let first = -Infinity;
  for (const time of [...times].sort((a, b) => a - b)) {
    if (time - first < merge) out[out.length - 1] = time;
    else {
      out.push(time);
      first = time;
    }
  }
  return out;
}

/** A latency axis from 0 to a round number just above `max`, with at most five steps. */
export function niceScale(max: number): { max: number; step: number } {
  const top = Math.max(1, max) * 1.08;
  const power = 10 ** Math.floor(Math.log10(top / 4));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => top / s <= 5) ?? power * 10;
  return { max: Math.ceil(top / step) * step, step };
}

const TICK_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 345600];
const two = (n: number) => String(n).padStart(2, '0');

/**
 * Time-axis ticks on round local-time boundaries, as many as fit `width` pixels.
 * `offset` is the local zone's offset from UTC in seconds (east positive); it defaults to the browser's.
 */
export function timeTicks(from: number, to: number, width: number, offset = -new Date(to * 1000).getTimezoneOffset() * 60): { time: number; label: string }[] {
  const span = to - from;
  const room = Math.max(2, Math.floor(width / 84));
  const step = TICK_STEPS.find((s) => span / s <= room) ?? TICK_STEPS[TICK_STEPS.length - 1];
  const ticks: { time: number; label: string }[] = [];
  for (let local = Math.ceil((from + offset) / step) * step; local <= to + offset; local += step) {
    const d = new Date(local * 1000);
    const date = `${two(d.getUTCMonth() + 1)}/${two(d.getUTCDate())}`;
    const clock = `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
    ticks.push({ time: local - offset, label: step >= 86400 || (span >= 86400 && local % 86400 === 0) ? date : clock });
  }
  return ticks;
}
