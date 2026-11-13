import { describe, expect, it } from 'vitest';
import type { LinkSeries, Point } from '$lib/types';
import { displayState, explain, niceScale, parseRange, pathSwitches, plotSeries, pointAt, rangeStats, stops, timeTicks } from './model';

const T = 1_794_550_000;

function point(index: number, over: Partial<Point> = {}): Point {
  const time = T + index * 10;
  return {
    time, end: time + 10, samples: 1, lost: 0, loss: 0, mean: 20 + index, peak: 20 + index, p95: 20 + index, jitter: null,
    status: 'ok', error: '', failures: 0, resolved_ip: '198.51.100.1', first_slot: time + 2, last_slot: time + 2,
    gap: false, endpoint_change: false, connect: index > 0, path_key: 'a', path_label: 'A → T', interval: 10, ...over,
  };
}
const empty = (index: number, over: Partial<Point> = {}) =>
  point(index, { samples: 0, loss: null, mean: null, peak: null, p95: null, status: 'missing', resolved_ip: '', first_slot: null, last_slot: null, connect: false, path_key: null, path_label: '', interval: null, ...over });
const timeout = (index: number) => point(index, { lost: 1, loss: 100, mean: null, peak: null, p95: null, status: 'timeout', failures: 1, gap: true, connect: false });

function series(points: Point[], over: Partial<LinkSeries> = {}): LinkSeries {
  return {
    protocol: 'tcp-connect-v1', state: 'collecting', note: '', path_key: 'a', points, step: 10,
    summary: { samples: 0, lost: 0, loss: null, mean: null, peak: null, p95: null, jitter: null }, latest: null, ...over,
  };
}
const plot = (points: Point[]) => plotSeries(series(points), T - 100, T + 1000);

describe('plotSeries', () => {
  it('joins continuous buckets into one line through their sample times', () => {
    const out = plot([point(0), point(1), point(2)]);
    expect(out.runs).toEqual([[T + 2, 20, T + 12, 21, T + 22, 22]]);
    expect(out.lone).toEqual([]);
    expect(out.dots.length).toBe(9);
    expect(out.max).toBe(22);
  });
  it('never draws across missing data', () => {
    const out = plot([point(0), point(1), empty(2), empty(3), point(4, { connect: false }), point(5)]);
    expect(out.runs).toEqual([[T + 2, 20, T + 12, 21], [T + 42, 24, T + 52, 25]]);
  });
  it('bridges an empty bucket only when the server marks the next sample as continuous', () => {
    expect(plot([point(0), empty(1), point(2, { connect: true })]).runs).toEqual([[T + 2, 20, T + 22, 22]]);
    expect(plot([point(0), empty(1), point(2, { connect: false })]).runs).toEqual([]);
  });
  it('breaks at timeouts, marks them, and keeps isolated buckets visible as dots', () => {
    const out = plot([point(0), timeout(1), point(2, { connect: false }), point(3)]);
    expect(out.runs).toEqual([[T + 22, 22, T + 32, 23]]);
    expect(out.lone).toEqual([T + 2, 20]);
    expect(out.fails).toEqual([T + 12, 100]);
  });
  it('breaks when the path or the resolved address changes', () => {
    expect(plot([point(0), point(1, { path_key: 'b' })]).runs).toEqual([]);
    expect(plot([point(0), point(1, { resolved_ip: '198.51.100.2' })]).runs).toEqual([]);
    expect(plot([point(0), point(1, { endpoint_change: true })]).lone.length).toBe(4);
  });
  it('shows the real extent of aggregated buckets and their peaks', () => {
    const wide = point(0, { first_slot: T, last_slot: T + 8, peak: 40 });
    const out = plot([wide]);
    expect(out.runs).toEqual([[T, 20, T + 4, 20, T + 8, 20]]);
    expect(out.max).toBe(40);
  });
  it('records unmeasurable intervals as merged spans, not failures', () => {
    const off = (index: number) => empty(index, { status: 'offline', failures: 1, gap: true, error: '主动 Agent 未连接' });
    const out = plot([point(0), off(1), off(2), point(3, { connect: false })]);
    expect(out.fails).toEqual([]);
    expect(out.idle).toEqual([T + 10, T + 30]);
  });
  it('ignores points outside the window', () => {
    expect(plotSeries(series([point(0), point(1)]), T + 5, T + 100).dots.length).toBe(3);
  });
});

describe('cursor helpers', () => {
  const points = [point(0), empty(1), point(2), point(4)];
  it('finds the bucket covering a time', () => {
    expect(pointAt(points, T + 3)?.time).toBe(T);
    expect(pointAt(points, T + 10)?.status).toBe('missing');
    expect(pointAt(points, T + 35)).toBeNull();
    expect(pointAt(points, T - 1)).toBeNull();
    expect(pointAt([], T)).toBeNull();
  });
  it('lists distinct times with data across series', () => {
    expect(stops([series(points), series([point(2), point(3)])], T, T + 100)).toEqual([T, T + 20, T + 30, T + 40]);
    const shifted = [point(0, { time: T + 1 }), point(1, { time: T + 11 })];
    expect(stops([series(points), series(shifted)], T, T + 100, 5)).toEqual([T + 1, T + 11, T + 20, T + 40]);
  });
  it('reports each path switch once', () => {
    const a = [point(0), point(1, { path_key: 'b', path_label: 'B → T' }), point(2, { path_key: 'b' })];
    const b = [point(0, { path_key: 'x' }), point(1, { path_key: 'y', path_label: 'B → T' })];
    expect(pathSwitches([series(a), series(b)], T, T + 100)).toEqual([{ time: T + 10, label: 'B → T' }]);
  });
});

describe('axes', () => {
  it('rounds the latency axis up to a readable maximum', () => {
    expect(niceScale(0)).toEqual({ max: 1.25, step: 0.25 });
    expect(niceScale(37)).toEqual({ max: 40, step: 10 });
    expect(niceScale(140)).toEqual({ max: 200, step: 50 });
    for (const max of [0.3, 3, 9.9, 47, 99, 101, 950, 12000]) {
      const scale = niceScale(max);
      expect(scale.max).toBeGreaterThanOrEqual(max);
      expect(scale.max / scale.step).toBeLessThanOrEqual(5.001);
    }
  });
  it('puts time ticks on round local boundaries', () => {
    const noon = Date.UTC(2026, 10, 13, 12, 7, 30) / 1000;
    expect(timeTicks(noon - 900, noon, 900, 0).map((tick) => tick.label)).toEqual(['11:54', '11:56', '11:58', '12:00', '12:02', '12:04', '12:06']);
    expect(timeTicks(noon - 900, noon, 300, 0).map((tick) => tick.label)).toEqual(['11:55', '12:00', '12:05']);
    expect(timeTicks(noon - 86400, noon, 600, 0).map((tick) => tick.label)).toEqual(['18:00', '11/13', '06:00', '12:00']);
    expect(timeTicks(noon - 604800, noon, 900, 8 * 3600).map((tick) => tick.label)).toEqual(['11/07', '11/08', '11/09', '11/10', '11/11', '11/12', '11/13']);
  });
});

describe('summaries and states', () => {
  it('aggregates the loaded range', () => {
    const stats = rangeStats([point(0, { samples: 3, mean: 10, peak: 30 }), point(1, { samples: 2, lost: 1, mean: 20, peak: 20 }), empty(2)]);
    expect(stats).toEqual({ samples: 5, lost: 1, loss: 20, mean: 12.5, peak: 30 });
    expect(rangeStats([])).toEqual({ samples: 0, lost: 0, loss: null, mean: null, peak: null });
  });
  it('uses the latest sample only while it is fresh and on the current path', () => {
    const latest = { task_id: 't', slot: T, status: 'timeout' as const, rtt: null, error: '', resolved_ip: '', epoch: null, previous_slot: null, path_key: 'a' };
    expect(displayState(series([], { latest }), T + 10)).toBe('timeout');
    expect(displayState(series([], { latest }), T + 60)).toBe('collecting');
    expect(displayState(series([], { latest: { ...latest, path_key: 'old' } }), T + 10)).toBe('collecting');
    expect(displayState(series([], { latest, state: 'offline' }), T + 10)).toBe('offline');
  });
  it('explains why a probe is not running and where to fix it', () => {
    const at = (state: LinkSeries['state'], note = '') => explain(series([], { state, note }), T);
    expect(at('unsupported', '请升级发起端 Agent')).toMatchObject({ label: '需要升级 Agent', action: 'node', measuring: false });
    expect(at('disabled', '节点已关闭质量探测')).toMatchObject({ label: '探测已关闭', action: 'node' });
    expect(at('offline', '主动 Agent 未连接')).toMatchObject({ label: '节点离线', tone: 'danger', action: 'node' });
    expect(at('waiting', '等待链路生效')).toMatchObject({ label: '等待探测就绪', action: 'link' });
    expect(at('waiting', '等待链路生效').text).toContain('等待链路生效');
    expect(at('unknown', '目标未提供 UDP 回显')).toMatchObject({ label: '不适用', applicable: false });
    expect(at('unknown', '无可探测的中间节点')).toMatchObject({ label: '无法探测', applicable: false });
    expect(at('unknown', '发起端不是已确认的主动节点')).toMatchObject({ label: '无法探测', applicable: true, action: 'node' });
    expect(at('collecting')).toMatchObject({ label: '持续采集中', measuring: true });
    expect(at('error', '端口被占用').text).toContain('端口被占用');
  });
  it('parses the range from the address', () => {
    expect(parseRange('3600')).toBe(3600);
    expect(parseRange('42')).toBe(900);
    expect(parseRange(null)).toBe(900);
  });
});
