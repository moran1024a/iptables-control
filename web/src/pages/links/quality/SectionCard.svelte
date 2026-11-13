<script lang="ts">
  // One measured stretch of a link (the whole path or a single hop): summary tiles, legend and chart.
  import Hourglass from '@lucide/svelte/icons/hourglass';
  import { clock } from '$lib/format';
  import { href } from '$lib/router.svelte';
  import type { LinkQuality, LinkSeries } from '$lib/types';
  import Badge from '../../../ui/Badge.svelte';
  import TagList from '../../../ui/TagList.svelte';
  import LatencyChart from './LatencyChart.svelte';
  import { explain, freshLatest, ms, pct, PROTOCOLS, rangeStats, type Explanation, type Protocol } from './model';

  type Props = {
    section: LinkQuality['sections'][number];
    now: number;
    range: number;
    rangeLabel: string;
    linkId: number;
    /** Protocols the user switched off in the legend (shared by every card on the page). */
    hidden: Protocol[];
    ontoggle: (protocol: Protocol) => void;
    /** The path has a single hop, so this card stands for both the whole path and that hop. */
    only?: boolean;
  };
  let { section, now, range, rangeLabel, linkId, hidden, ontoggle, only = false }: Props = $props();

  let focus = $state<Protocol | null>(null);

  const source = $derived(section.labels.find((label) => label.node_id != null));
  const items = $derived(
    section.series.map((series) => {
      const info = explain(series, now);
      const latest = freshLatest(series, now);
      return { series, info, latest, hasData: series.points.some((point) => point.mean != null), stats: rangeStats(series.points) };
    }),
  );
  /** Series worth a line: everything that can measure this stretch, plus anything that has history. */
  const charted = $derived(items.filter((item) => item.info.applicable || item.hasData));
  const visible = $derived<LinkSeries[]>(charted.filter((item) => !hidden.includes(item.series.protocol)).map((item) => item.series));
  const hasData = $derived(charted.some((item) => item.hasData));
  /** When every usable probe is held up for the same reason, say it once instead of three times. */
  const blocked = $derived.by<Explanation | null>(() => {
    const pool = charted.length ? charted : items;
    const first = pool[0]?.info;
    if (!first || pool.some((item) => item.info.measuring || item.series.summary.samples > 0)) return null;
    return pool.every((item) => item.info.label === first.label && item.info.text === first.text) ? first : null;
  });
  const showChart = $derived(hasData || charted.some((item) => item.info.measuring));
</script>

{#snippet action(info: Explanation)}
  {#if info.action === 'node' && source}
    <a href={href(`nodes/${source.node_id}`)} class="font-medium break-words text-accent-text hover:underline">打开节点「{source.name}」</a>
  {:else if info.action}
    <a href={href(`routes/${linkId}`)} class="font-medium break-words text-accent-text hover:underline">打开链路</a>
  {/if}
{/snippet}

<section class="rounded-lg border bg-surface shadow-sm" aria-label="{section.kind} {section.label}">
  <header class="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 border-b px-4 py-2.5">
    <Badge tone={section.kind === '全段' ? 'accent' : 'idle'}>{only ? '全段 · 仅一段' : section.kind}</Badge>
    <h2 class="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-[0.8125rem] font-semibold">
      {#each section.labels as label, index (index)}
        {#if index}<span class="font-normal text-fg-subtle" aria-hidden="true">→</span>{/if}
        <span class="inline-flex min-w-0 items-center gap-1.5">
          <span class="truncate">{label.name}</span>
          {#if label.tags?.length}<TagList tags={label.tags} limit={2} expandable={false} />{/if}
          {#if label.address}<span class="tabular truncate text-xs font-normal text-fg-muted">{label.address.host}:{label.address.port}</span>{/if}
        </span>
      {:else}
        {section.label}
      {/each}
    </h2>
  </header>

  <div class="flex flex-col gap-4 p-4">
    {#if blocked}
      <div class="flex items-start gap-3 rounded-lg bg-surface-2 px-4 py-3.5">
        <div class="flex size-9 shrink-0 items-center justify-center rounded-lg bg-surface-3 text-fg-muted"><Hourglass class="size-4" /></div>
        <div class="min-w-0 text-[0.8125rem] leading-relaxed">
          <p class="font-medium">{blocked.label}</p>
          <p class="text-fg-muted">{blocked.text} {@render action(blocked)}</p>
        </div>
      </div>
    {:else}
      <div class="grid gap-3 lg:grid-cols-3">
        {#each items as { series, info, latest, stats } (series.protocol)}
          {@const summary = series.summary}
          <div class="flex min-w-0 flex-col rounded-lg border px-3.5 py-3 {info.applicable ? '' : 'bg-surface-2'}">
            <div class="flex items-center justify-between gap-2">
              <span class="inline-flex items-center gap-2 text-[0.8125rem] font-semibold">
                <span class="h-[3px] w-3.5 rounded-full {PROTOCOLS[series.protocol].swatch}"></span>{PROTOCOLS[series.protocol].name}
              </span>
              <span class="flex min-w-0 items-center gap-2">
                {#if series.latest && info.applicable}<span class="tabular text-xs text-fg-subtle" title="最近一次探测的时间">{clock(series.latest.slot)}</span>{/if}
                <Badge tone={info.tone} dot={info.measuring}>{info.label}</Badge>
              </span>
            </div>
            {#if info.measuring || summary.samples > 0}
              <p class="mt-2 flex items-baseline gap-2">
                <span class="tabular text-2xl leading-8 font-semibold tracking-tight">{latest?.status === 'ok' ? ms(latest.rtt) : '—'}</span>
                <span class="text-xs text-fg-muted">最新延迟</span>
              </p>
              <dl class="mt-2 grid grid-cols-4 gap-x-3 text-xs">
                <div><dt class="text-fg-muted">5 分钟平均</dt><dd class="tabular mt-0.5 text-[0.8125rem] font-medium">{ms(summary.mean)}</dd></div>
                <div><dt class="text-fg-muted">P95</dt><dd class="tabular mt-0.5 text-[0.8125rem] font-medium">{ms(summary.p95)}</dd></div>
                <div><dt class="text-fg-muted">波动</dt><dd class="tabular mt-0.5 text-[0.8125rem] font-medium">{ms(summary.jitter)}</dd></div>
                <div>
                  <dt class="text-fg-muted">超时率</dt>
                  <dd class="tabular mt-0.5 text-[0.8125rem] font-medium {(summary.loss ?? 0) > 0 ? 'text-warn-text' : ''}" title={summary.samples ? `${summary.lost} / ${summary.samples} 个样本` : undefined}>
                    {pct(summary.loss)}
                  </dd>
                </div>
              </dl>
              <p class="mt-2 border-t pt-2 text-xs text-fg-muted">
                {#if range > 900 && stats.samples}
                  {rangeLabel}：平均 <span class="tabular text-fg">{ms(stats.mean)}</span> · 峰值 <span class="tabular text-fg">{ms(stats.peak)}</span> · 超时
                  <span class="tabular text-fg">{pct(stats.loss)}</span>（{stats.lost} / {stats.samples}）
                {:else}
                  近 5 分钟 {summary.lost} / {summary.samples} 个样本超时
                {/if}
              </p>
            {/if}
            {#if info.text}
              <p class="mt-2 min-w-0 text-xs leading-relaxed break-words text-fg-muted">{info.text} {@render action(info)}</p>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    {#if showChart}
      <div>
        <div class="mb-1 flex flex-wrap items-center gap-x-1 gap-y-1 text-xs">
          {#each charted as { series } (series.protocol)}
            {@const off = hidden.includes(series.protocol)}
            <button
              type="button"
              aria-pressed={!off}
              aria-label="{off ? '显示' : '隐藏'} {PROTOCOLS[series.protocol].name} 曲线"
              title={off ? '点击显示这条曲线' : '点击隐藏这条曲线'}
              class="inline-flex h-6 items-center gap-1.5 rounded-md px-2 font-medium hover:bg-surface-3 {off ? 'text-fg-subtle line-through' : 'text-fg'}"
              onclick={() => ontoggle(series.protocol)}
              onpointerenter={() => (focus = off ? null : series.protocol)}
              onpointerleave={() => (focus = null)}
              onfocus={() => (focus = off ? null : series.protocol)}
              onblur={() => (focus = null)}
            >
              <span class="h-[3px] w-3.5 rounded-full {PROTOCOLS[series.protocol].swatch} {off ? 'opacity-30' : ''}"></span>{PROTOCOLS[series.protocol].name}
            </button>
          {/each}
          <span class="ml-auto flex flex-wrap items-center gap-x-3 text-fg-muted">
            <span><span class="font-semibold" aria-hidden="true">×</span> 超时或异常</span>
            <span><span aria-hidden="true">┆</span> 路径切换</span>
            <span class="max-sm:hidden">细线为峰值</span>
          </span>
        </div>
        {#if visible.length}
          <LatencyChart series={visible} {now} {range} {focus} label={section.label} />
        {:else}
          <p class="rounded-lg bg-surface-2 px-4 py-10 text-center text-[0.8125rem] text-fg-muted">所有曲线都已隐藏，点击上方的协议名称重新显示。</p>
        {/if}
      </div>
    {/if}
  </div>
</section>
