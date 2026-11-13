<script lang="ts">
  // Network quality of one link: #/routes/<id>/quality?range=<seconds>&config=<configuration id>
  import Activity from '@lucide/svelte/icons/activity';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import Unlink from '@lucide/svelte/icons/unlink';
  import { untrack } from 'svelte';
  import { api, ApiError } from '$lib/api';
  import { clock } from '$lib/format';
  import { usePoll } from '$lib/poll.svelte';
  import { router } from '$lib/router.svelte';
  import { session } from '$lib/session.svelte';
  import type { LinkQuality, Range } from '$lib/types';
  import Page from '../../layout/Page.svelte';
  import Alert from '../../ui/Alert.svelte';
  import Button from '../../ui/Button.svelte';
  import EmptyState from '../../ui/EmptyState.svelte';
  import Segmented from '../../ui/Segmented.svelte';
  import Select from '../../ui/Select.svelte';
  import Spinner from '../../ui/Spinner.svelte';
  import { parseRange, RANGES, type Protocol } from './quality/model';
  import SectionCard from './quality/SectionCard.svelte';

  let { id }: { id: number } = $props();

  // The view lives in the address, so a reload or a shared link shows the same thing.
  const range = $derived(parseRange(router.query.get('range')));
  const wanted = $derived.by(() => {
    const raw = router.query.get('config');
    return raw && /^\d+$/.test(raw) ? Number(raw) : null;
  });
  function show(next: { range?: Range; config?: number | null }) {
    const query = new URLSearchParams();
    const nextRange = next.range ?? range;
    const nextConfig = next.config === undefined ? wanted : next.config;
    if (nextRange !== 900) query.set('range', String(nextRange));
    if (nextConfig) query.set('config', String(nextConfig));
    const text = query.toString();
    router.go(`routes/${id}/quality${text ? `?${text}` : ''}`, { replace: true });
  }

  let missing = $state(false);
  let hidden = $state<Protocol[]>([]);

  const request = (seconds: Range, configuration: number | null, signal: AbortSignal) =>
    // quiet: this endpoint answers 401 for "must change password" too, so the session is re-read below.
    api<LinkQuality>('POST', 'quality', { scope: 'topologies', id, range: seconds, ...(configuration ? { configuration } : {}) }, { signal, quiet: true });

  const poll = usePoll(
    async (signal) => {
      const seconds = range;
      const configuration = wanted;
      try {
        let quality: LinkQuality;
        try {
          quality = await request(seconds, configuration, signal);
        } catch (err) {
          // The remembered configuration no longer belongs to this link: fall back to the default one.
          if (!(err instanceof ApiError && err.field === 'configuration' && configuration)) throw err;
          quality = await request(seconds, null, signal);
          show({ config: null });
        }
        // Keep the range next to the data it was loaded for, so a range switch never redraws old data on a new axis.
        return { quality, range: seconds };
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) missing = true;
        if (err instanceof ApiError && err.status === 401) void session.load();
        throw err;
      }
    },
    { interval: 10000, paused: () => missing },
  );

  let started = false;
  $effect(() => {
    void range;
    void wanted;
    if (!started) {
      started = true;
      return;
    }
    untrack(() => void poll.refresh());
  });

  const quality = $derived(poll.data?.quality);
  const loadedRange = $derived(poll.data?.range ?? range);
  const switching = $derived(!!poll.data && !poll.error && (poll.data.range !== range || (wanted != null && quality?.configuration !== wanted)));
  const rangeLabel = $derived(RANGES.find((item) => item.value === loadedRange)?.label ?? '');
  const configName = $derived(quality?.configurations.find((item) => item.id === quality.configuration)?.name ?? '');
  /** With a single hop the whole path and that hop are the same measurement; show it once. */
  const sections = $derived.by(() => {
    const all = quality?.sections ?? [];
    return all.length === 2 && all[0].label === all[1].label ? all.slice(0, 1) : all;
  });
  const merged = $derived(!!quality && sections.length < quality.sections.length);

  function toggle(protocol: Protocol) {
    hidden = hidden.includes(protocol) ? hidden.filter((item) => item !== protocol) : [...hidden, protocol];
  }
</script>

<Page
  title={quality ? `网络质量 · ${quality.name}` : '网络质量'}
  subtitle={quality && !missing ? `${configName ? `${configName} · ` : ''}每 ${quality.interval} 秒探测一次 · 更新于 ${clock(quality.now)}` : undefined}
  back="routes/{id}"
  backLabel="返回链路"
>
  {#snippet actions()}
    {#if quality && !missing}
      {#if switching}<Spinner class="size-4 text-fg-muted" />{/if}
      {#if quality.configurations.length > 1}
        <Select
          aria-label="配置"
          class="max-w-44"
          value={quality.configuration ?? undefined}
          options={quality.configurations.map((item) => ({ value: item.id, label: item.name }))}
          onchange={(event) => show({ config: Number(event.currentTarget.value) })}
        />
      {/if}
      <Segmented label="时间范围" value={range} options={RANGES} onchange={(value) => show({ range: value })} />
    {/if}
  {/snippet}

  {#if missing}
    <EmptyState icon={Unlink} title="链路不存在" text="该链路可能已被删除。">
      <Button href="#/routes">返回链路列表</Button>
    </EmptyState>
  {:else if !quality}
    {#if poll.error}
      <Alert tone="danger" title="无法加载网络质量">
        {poll.error}
        {#snippet actions()}<Button size="sm" onclick={() => poll.refresh()}><RefreshCw class="size-3.5" />重试</Button>{/snippet}
      </Alert>
    {:else}
      <div class="flex items-center justify-center gap-2 py-20 text-[0.8125rem] text-fg-muted"><Spinner />正在加载…</div>
    {/if}
  {:else}
    {#if poll.error}
      <Alert tone="warn" title="刷新失败，显示的是 {clock(quality.now)} 的数据">
        {poll.error}
        {#snippet actions()}<Button size="sm" onclick={() => poll.refresh()}><RefreshCw class="size-3.5" />重试</Button>{/snippet}
      </Alert>
    {/if}
    {#if !sections.length}
      <div class="rounded-lg border bg-surface shadow-sm">
        <EmptyState icon={Activity} title="这条链路还没有可测量的配置" text="网络质量按配置（入口到目标的一条路径）测量。先在链路里连好入口、中间节点和目标并保存，这里就会显示每一段的延迟和超时。">
          <Button href="#/routes/{id}">打开链路</Button>
        </EmptyState>
      </div>
    {:else}
      {#each sections as section (section.id)}
        <SectionCard {section} now={quality.now} range={loadedRange} {rangeLabel} linkId={id} {hidden} ontoggle={toggle} only={merged} />
      {/each}
      <p class="text-xs leading-relaxed text-fg-subtle">
        探测由路径上的第一个中间节点发起，入口节点不参与。最新延迟、5 分钟平均、P95、波动和超时率始终统计最近 5 分钟；图表按所选时间范围聚合显示。
      </p>
    {/if}
  {/if}
</Page>
