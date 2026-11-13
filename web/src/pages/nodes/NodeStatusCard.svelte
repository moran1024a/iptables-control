<script lang="ts">
  import { ago } from '$lib/format';
  import type { NodeDetail } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import StatusBar from '$ui/StatusBar.svelte';
  import CommandBlock from './CommandBlock.svelte';
  import { AGENT_UPDATE_TEXT, installed } from './node-logic';

  type Props = { detail: NodeDetail; now: number; busy?: boolean; onforce: () => void };
  let { detail, now, busy = false, onforce }: Props = $props();
  const node = $derived(detail.node);
  const passive = $derived(node.desired_mode === 'passive');
  const manual = $derived(passive || node.manual_required);
  const ready = $derived(installed(node));

  type Summary = { title: string; text: string; command?: string };
  /** What the current state means and what, if anything, the operator should do next. */
  const summary = $derived.by((): Summary => {
    const seen = ago(node.last_seen, now, '');
    switch (node.status) {
      case 'deleting':
        return { title: '正在删除', text: '已向节点下发空配置，节点确认清空规则后会自动移除。如果节点已永久失联，可以强制删除。' };
      case 'error':
        if (node.wake && (node.wake.state === 'failed' || node.wake.state === 'timeout'))
          return { title: '远程唤醒未成功', text: '节点没有确认当前变更。请在节点上手动同步，并检查唤醒端口与白名单设置。', command: 'sudo ictl --sync' };
        return { title: '节点报告异常', text: '节点无法按当前配置工作，原因见下方错误信息。排除问题后状态会自动恢复。' };
      case 'syncing':
        if (!ready) return { title: '等待安装', text: '节点尚未安装，请在节点上执行上方的一键接入命令。' };
        if (node.wake?.state === 'waiting')
          return { title: '已发送远程唤醒', text: '正在等待节点确认同步；超过 30 秒未确认时转为等待手动同步。', command: 'sudo ictl --sync' };
        if (manual)
          return {
            title: node.manual_required && !passive ? '等待在节点上确认模式切换' : '等待节点手动同步',
            text: '在节点上运行下面的命令，或在 ictl 菜单中选择「同步更新」。等待状态会持续到当前变更确认成功。',
            command: 'sudo ictl --sync',
          };
        return { title: '正在同步', text: `节点尚未确认最新配置（确认进度 ${node.applied_generation} / ${node.desired_generation}），通常在几秒内完成。` };
      case 'synced':
        return passive
          ? { title: '已同步', text: '最近一次确认结果与当前配置一致。被动模式不自动轮询，这不表示节点实时在线。' }
          : { title: '运行正常', text: '节点在线，配置已同步。' };
      case 'offline':
        return {
          title: '节点失联',
          text: `超过 30 秒没有收到上报${seen ? `（最后上报 ${seen}）` : ''}。请检查节点的网络和 Agent 是否在运行；重新执行一键接入命令可以修复安装。`,
        };
      case 'skew':
        return {
          title: '节点时间不一致',
          text: `节点时钟与面板相差${node.skew === null ? '' : ` ${Math.abs(node.skew)} 秒`}（超过 60 秒），同步已暂停。请在节点上校准时间，例如启用 NTP。`,
        };
      case 'new':
        return ready
          ? { title: '尚未连接', text: '节点还没有连接到面板。请确认节点能访问同步地址。' }
          : { title: '尚未接入', text: '节点还没有安装并连接到面板，请在节点上执行上方的一键接入命令。' };
      default:
        return { title: String(node.status), text: '' };
    }
  });

  const wakeText = $derived.by(() => {
    if (!passive) return '';
    if (!node.wake_enabled) return '远程唤醒未开启，配置变更需要在节点上手动同步。';
    const state =
      node.wake?.state === 'waiting'
        ? '正在等待同步确认，超过 30 秒转为等待手动同步。'
        : node.wake?.state === 'confirmed'
          ? '最近一次同步已确认。'
          : '请确保节点本地已配置白名单和相同端口。';
    return `远程唤醒已开启，UDP ${node.wake_port}。${state}`;
  });
  const updateText = $derived(AGENT_UPDATE_TEXT[node.agent_update] ?? '');
  const LEGEND = {
    active: [['bg-ok', '正常'], ['bg-warn', '故障'], ['bg-danger', '失联'], ['bg-idle/30', '无数据']],
    passive: [['bg-ok', '有手动同步或远程同步成功'], ['bg-surface ring-1 ring-inset ring-border-strong/70', '无同步活动']],
  } as const;
</script>

<section class="flex flex-col gap-3.5 rounded-lg border bg-surface p-4 shadow-sm">
  <div class="flex flex-col gap-1.5">
    <h2 class="text-[0.9375rem] font-semibold">{summary.title}</h2>
    {#if summary.text}<p class="text-[0.8667rem] leading-relaxed text-fg-muted">{summary.text}</p>{/if}
    {#if summary.command}<CommandBlock text={summary.command} prompt class="mt-1 max-w-md" />{/if}
    {#if node.status === 'deleting'}
      <div class="mt-1"><Button variant="danger-ghost" size="sm" class="border border-danger/30" disabled={busy} onclick={onforce}>强制删除</Button></div>
    {/if}
  </div>

  {#if node.err}<Alert tone="danger" title="最近错误">{node.err}</Alert>{/if}
  {#if node.wake?.error}<Alert tone="warn" title="远程唤醒">{node.wake.error}</Alert>{/if}

  <div>
    <StatusBar bar={node.bar} axis />
    <ul class="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-2xs text-fg-muted">
      {#each LEGEND[passive ? 'passive' : 'active'] as [color, label] (label)}
        <li class="inline-flex items-center gap-1"><span class="size-2 rounded-[2px] {color}"></span>{label}</li>
      {/each}
      {#if passive}<li>执行结果见事件记录</li>{/if}
    </ul>
  </div>

  {#if wakeText || updateText || (node.manual_required && node.status !== 'syncing')}
    <ul class="flex flex-col gap-1 border-t pt-3 text-xs leading-relaxed text-fg-muted">
      {#if wakeText}<li>{wakeText}</li>{/if}
      {#if updateText}
        <li>节点端版本 {node.agent ?? '-'}，目标版本 {detail.forward_version}：{updateText}。</li>
      {/if}
      {#if node.manual_required && node.status !== 'syncing'}<li>模式切换待确认：请在节点上运行 sudo ictl --sync。</li>{/if}
    </ul>
  {/if}
</section>
