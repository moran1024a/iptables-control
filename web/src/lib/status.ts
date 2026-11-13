import type { Tone } from '$ui/Badge.svelte';
import type { NodeStatus } from './types';

export const NODE_STATUS: Record<NodeStatus, { label: string; tone: Tone; live?: boolean }> = {
  new: { label: '未连接', tone: 'idle' },
  offline: { label: '离线', tone: 'danger' },
  skew: { label: '时间不一致', tone: 'warn' },
  syncing: { label: '等待中', tone: 'info', live: true },
  synced: { label: '已同步', tone: 'ok' },
  error: { label: '异常', tone: 'warn' },
  deleting: { label: '删除中', tone: 'idle', live: true },
};
export const NODE_STATUS_ORDER: NodeStatus[] = ['new', 'offline', 'skew', 'syncing', 'synced', 'error', 'deleting'];
export const MODE_LABEL = { active: '主动', passive: '被动' } as const;

/** Links and their topology configurations share one set of states. */
export const LINK_STATE: Record<string, { label: string; tone: Tone; live?: boolean }> = {
  active: { label: '已生效', tone: 'ok' },
  pending: { label: '已保存', tone: 'warn', live: true },
  preparing: { label: '准备下游', tone: 'warn', live: true },
  switching: { label: '切换入口', tone: 'warn', live: true },
  waiting: { label: '等待同步', tone: 'warn', live: true },
  deleting: { label: '删除中', tone: 'idle', live: true },
  error: { label: '异常', tone: 'danger', live: true },
  imported: { label: '已导入', tone: 'info' },
};
/** Bucket used by state filters. */
export function linkStateGroup(state: string): string {
  if (['error', 'timeout', 'failed', 'offline'].includes(state)) return 'error';
  if (['preparing', 'switching', 'pending', 'waiting', 'manual'].includes(state)) return 'pending';
  return state;
}
