<script lang="ts" module>
  // Nodes whose retired-rule traffic rows are expanded; kept for this tab so a refresh does not fold them again.
  const expanded = new Set<number>();
</script>

<script lang="ts">
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import Stethoscope from '@lucide/svelte/icons/stethoscope';
  import { bytes, clock, dateTime, percent, rate } from '$lib/format';
  import type { NodeDetail } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import Disclosure from '$ui/Disclosure.svelte';
  import { recentHistory, usedPercent } from './node-logic';
  import Sparkline from './Sparkline.svelte';
  import StatTile from './StatTile.svelte';

  type Props = { detail: NodeDetail; ondiagnose?: () => void };
  let { detail, ondiagnose }: Props = $props();
  const node = $derived(detail.node);
  const t = $derived(detail.telemetry);
  const metrics = $derived(t.metrics ?? {});
  const inventory = $derived(t.inventory ?? {});
  const live = $derived(node.desired_mode === 'active' && node.status !== 'offline' && node.status !== 'new' && !!t.metrics_at && detail.now - t.metrics_at < 90);

  const memory = $derived(metrics.memory ?? {});
  const memoryUsed = $derived(usedPercent(memory.MemAvailable, memory.MemTotal));
  const diskUsed = $derived(usedPercent(metrics.disk?.available, metrics.disk?.total));
  const has = (v: unknown) => v !== null && v !== undefined && v !== '';
  const conntrack = $derived(
    has(metrics.conntrack_count) && Number(metrics.conntrack_max) > 0 ? Math.min(100, (Number(metrics.conntrack_count) / Number(metrics.conntrack_max)) * 100) : null,
  );
  const load = $derived(Array.isArray(metrics.load) && metrics.load.length ? metrics.load.slice(0, 3).join(' ') : '');
  const history = $derived(recentHistory(t.history ?? [], detail.now));
  const sampled = $derived(history.filter((slot) => slot.samples > 0));

  const ruleName = $derived(new Map(detail.rules.map((rule) => [rule.id, `${rule.configuration_name} · ${rule.port}`])));
  const interfaces = $derived(Object.entries(metrics.interfaces ?? {}));
  const ports = $derived(t.ports?.rows ?? []);
  const key = $derived(String(node.id));

  // Traffic of rules still on the node, by name; rows of rules that no longer exist are folded away by default.
  const currentTraffic = $derived(
    (t.traffic ?? []).filter((row) => ruleName.has(row.rule_id)).sort((a, b) => ruleName.get(a.rule_id)!.localeCompare(ruleName.get(b.rule_id)!, 'zh') || a.rule_id - b.rule_id || a.protocol.localeCompare(b.protocol)),
  );
  const oldTraffic = $derived((t.traffic ?? []).filter((row) => !ruleName.has(row.rule_id)));
  const total = (rows: typeof oldTraffic, field: 'up' | 'down') => {
    try {
      return rows.reduce((sum, row) => sum + BigInt(row[field] || 0), 0n);
    } catch {
      return null;
    }
  };
  let showOld = $state(false);
  $effect(() => {
    showOld = expanded.has(node.id);
  });
  function toggleOld() {
    showOld = !showOld;
    if (showOld) expanded.add(node.id);
    else expanded.delete(node.id);
  }
</script>

<Card title="系统与流量" hint="主动 Agent 采集 · 最近采样 {dateTime(t.metrics_at)}">
  {#snippet actions()}
    {#if ondiagnose}<Button size="sm" onclick={ondiagnose}><Stethoscope />诊断</Button>{/if}
  {/snippet}
  <div class="flex flex-col gap-4">
    {#if !live}
      <Alert tone="info">以下是最近一次采样的历史值；节点离线或切为被动后不再更新，不代表实时状态。</Alert>
    {/if}
    {#if metrics.traffic_quality}<Alert tone="warn">{metrics.traffic_quality}</Alert>{/if}
    {#if metrics.error}<Alert tone="warn" title="采集错误">{metrics.error}</Alert>{/if}

    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatTile
        label="CPU"
        value={percent(metrics.cpu_percent)}
        percent={metrics.cpu_percent ?? null}
        detail={[has(inventory.cpus) ? `${inventory.cpus} 核` : '', load ? `负载 ${load}` : ''].filter(Boolean).join(' · ') || undefined}
      >
        <Sparkline
          class="mt-auto"
          values={history.map((slot) => slot.cpu)}
          label="最近 20 分钟 CPU 均值"
          describe={(i) => `${clock(history[i].time)}  CPU ${percent(history[i].cpu)}`}
        />
      </StatTile>
      <StatTile
        label="内存"
        value={percent(memoryUsed)}
        percent={memoryUsed}
        detail={has(memory.MemTotal) ? `可用 ${bytes(memory.MemAvailable)} / 共 ${bytes(memory.MemTotal)}` : undefined}
      >
        <Sparkline
          class="mt-auto"
          values={history.map((slot) => slot.memory)}
          label="最近 20 分钟内存使用率均值"
          describe={(i) => `${clock(history[i].time)}  内存 ${percent(history[i].memory)}`}
        />
      </StatTile>
      <StatTile
        label="磁盘"
        value={percent(diskUsed)}
        percent={diskUsed}
        detail={metrics.disk ? `可用 ${bytes(metrics.disk.available)} / 共 ${bytes(metrics.disk.total)}` : undefined}
      />
      <StatTile
        label="连接跟踪"
        value={has(metrics.conntrack_count) ? String(metrics.conntrack_count) : '-'}
        percent={conntrack}
        detail={has(metrics.conntrack_max) ? `上限 ${metrics.conntrack_max}` : undefined}
      />
    </div>
    <p class="-mt-1 text-xs text-fg-muted">
      内核 / 架构：<span class="font-mono text-fg">{inventory.kernel || '-'} / {inventory.arch || '-'}</span>
      {#if sampled.length}<span class="text-fg-subtle"> · 趋势线为最近 20 分钟的每分钟均值，缺失的时间段留空</span>{/if}
    </p>

    <div>
      <h3 class="text-[0.8667rem] font-semibold">转发累计流量</h3>
      <p class="mt-0.5 mb-2 text-xs leading-relaxed text-fg-muted">
        按本节点每条规则的 IPv4 转发字节计数，包含协议头及重传；多跳分别计数。上行为入口到目标，下行为目标返回。主机网卡流量见「网卡与端口」。
      </p>
      <div class="overflow-x-auto rounded-md border">
        <table class="data-table">
          <thead><tr><th>规则</th><th>协议</th><th class="num">上行</th><th class="num">下行</th><th>计数状态</th></tr></thead>
          <tbody>
            {#snippet line(row: (typeof oldTraffic)[number], name: string)}
              <tr>
                <td>{name}</td>
                <td class="uppercase">{row.protocol}</td>
                <td class="num" title="{row.up} 字节">{bytes(row.up)}</td>
                <td class="num" title="{row.down} 字节">{bytes(row.down)}</td>
                <td class={row.quality === 'ok' ? 'text-fg-muted' : 'text-warn-text'}>{row.quality === 'ok' ? '正常' : row.quality}</td>
              </tr>
            {/snippet}
            {#each currentTraffic as row (`${row.rule_id}-${row.protocol}`)}
              {@render line(row, ruleName.get(row.rule_id)!)}
            {:else}
              <tr><td colspan="5" class="text-center text-fg-muted">{oldTraffic.length ? '当前没有规则' : '等待采集'}</td></tr>
            {/each}
            {#if oldTraffic.length}
              {@const up = total(oldTraffic, 'up')}
              {@const down = total(oldTraffic, 'down')}
              <tr>
                <td colspan="5" class="!p-0">
                  <button type="button" aria-expanded={showOld} onclick={toggleOld} class="flex w-full items-center gap-1.5 px-4 py-2 text-left text-xs text-fg-muted hover:bg-surface-2 hover:text-fg">
                    <ChevronDown class="size-3.5 shrink-0 transition-transform {showOld ? '' : '-rotate-90'}" />
                    <span class="font-medium">历史规则</span>
                    <span class="tabular">{oldTraffic.length} 条{#if up !== null && down !== null}{" · "}合计上行 {bytes(up)} / 下行 {bytes(down)}{/if}</span>
                  </button>
                </td>
              </tr>
              {#if showOld}
                {#each oldTraffic as row (`${row.rule_id}-${row.protocol}`)}
                  {@render line(row, `历史规则 ${row.rule_id}`)}
                {/each}
              {/if}
            {/if}
          </tbody>
        </table>
      </div>
    </div>

    <div class="-mx-4 -mb-4 flex flex-col divide-y border-t">
      <Disclosure title="网卡与端口" hint="{interfaces.length} 个网卡 · {t.ports?.total ?? ports.length} 个端口" key="network-{key}" card={false}>
        <div class="flex flex-col gap-3 px-4 pb-2">
          <div class="overflow-x-auto rounded-md border">
            <table class="data-table">
              <thead><tr><th>网卡</th><th class="num">接收</th><th class="num">发送</th><th class="num">接收速率</th><th class="num">发送速率</th></tr></thead>
              <tbody>
                {#each interfaces as [name, nic] (name)}
                  <tr>
                    <td class="font-mono">{name}</td>
                    <td class="num">{bytes(nic.rx_bytes)}</td>
                    <td class="num">{bytes(nic.tx_bytes)}</td>
                    <td class="num">{rate(nic.rx_bytes_per_second)}</td>
                    <td class="num">{rate(nic.tx_bytes_per_second)}</td>
                  </tr>
                {:else}
                  <tr><td colspan="5" class="text-center text-fg-muted">暂无网卡数据</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="text-xs text-fg-muted">
            端口快照（最多显示 100 条{ports.length > 100 || t.ports?.truncated ? `，共 ${t.ports?.total ?? ports.length} 条` : ''}）
          </p>
          {#each t.ports?.errors ?? [] as error (error)}<Alert tone="warn">{error}</Alert>{/each}
          <div class="max-h-80 overflow-auto rounded-md border">
            <table class="data-table">
              <thead><tr><th>来源</th><th>协议</th><th class="num">端口</th><th>地址</th></tr></thead>
              <tbody>
                {#each ports.slice(0, 100) as row, index (index)}
                  <tr>
                    <td>{row.source === 'ictl' ? 'ictl 转发' : row.source === 'socket' ? '本机监听' : String((row as { source: unknown }).source ?? '-')}</td>
                    <td class="uppercase">{row.protocol}</td>
                    <td class="num">{row.port}</td>
                    <td class="font-mono">{('address' in row && row.address) || ('target' in row && row.target) || '-'}</td>
                  </tr>
                {:else}
                  <tr><td colspan="4" class="text-center text-fg-muted">暂无端口数据</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>
      </Disclosure>
      <Disclosure title="最近 20 分钟（采样均值）" hint="{sampled.length} / 20 分钟有采样" key="history-{key}" card={false}>
        <div class="px-4 pb-2">
        <p class="mb-2 text-xs text-fg-muted">缺失的时间段没有采样，不按零处理。</p>
        <div class="max-h-80 overflow-auto rounded-md border">
          <table class="data-table">
            <thead><tr><th>时间</th><th class="num">CPU</th><th class="num">内存使用率</th><th class="num">样本数</th></tr></thead>
            <tbody>
              {#each [...history].reverse() as slot (slot.time)}
                <tr class={slot.samples ? '' : 'text-fg-subtle'}>
                  <td class="tabular">{dateTime(slot.time)}</td>
                  <td class="num">{percent(slot.cpu)}</td>
                  <td class="num">{percent(slot.memory)}</td>
                  <td class="num">{slot.samples || '无采样'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        </div>
      </Disclosure>
    </div>
  </div>
</Card>
