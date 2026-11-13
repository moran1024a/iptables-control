<script lang="ts">
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import { get } from '$lib/api';
  import { ago, clock } from '$lib/format';
  import { usePoll } from '$lib/poll.svelte';
  import { href } from '$lib/router.svelte';
  import { linkStateGroup } from '$lib/status';
  import type { NodesList, SubscriptionsList, TopologiesList } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Card from '$ui/Card.svelte';
  import EmptyState from '$ui/EmptyState.svelte';
  import LinkStateBadge from '$ui/LinkStateBadge.svelte';
  import NodeStatusBadge from '$ui/NodeStatusBadge.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import StatusBar from '$ui/StatusBar.svelte';
  import Page from '../../layout/Page.svelte';

  // One status summary built from the lists the other pages already use.
  const poll = usePoll(
    async (signal) => {
      const [nodes, links, subscriptions] = await Promise.all([
        get<NodesList>('nodes', { signal }),
        get<TopologiesList>('topologies', { signal }),
        get<SubscriptionsList>('subscriptions', { signal }).catch(() => null),
      ]);
      return { nodes, links, subscriptions };
    },
    { interval: 5000 },
  );
  const data = $derived(poll.data);
  const nodes = $derived(data?.nodes.nodes ?? []);
  const links = $derived(data?.links.topologies.filter((link) => !link.deleted) ?? []);
  const configs = $derived(links.flatMap((link) => link.configurations.filter((config) => config.state !== 'deleting')));
  const subscriptions = $derived(data?.subscriptions?.subscriptions ?? []);

  const count = <T,>(list: T[], test: (item: T) => boolean) => list.filter(test).length;
  const tiles = $derived([
    { label: '节点', page: 'nodes', value: count(nodes, (n) => n.status === 'synced'), total: nodes.length, unit: '已同步' },
    { label: '链路', page: 'routes', value: count(links, (l) => l.state === 'active'), total: links.length, unit: '已生效' },
    { label: '配置', page: 'configs', value: count(configs, (c) => c.state === 'active'), total: configs.length, unit: '已生效' },
    { label: '订阅', page: 'subscriptions', value: count(subscriptions, (s) => s.enabled), total: subscriptions.length, unit: '已启用' },
  ]);

  const NODE_HINT: Record<string, string> = { offline: '节点离线', skew: '节点时间与服务端不一致', new: '尚未连接，等待安装 Agent', syncing: '等待节点确认配置' };
  const nodeIssues = $derived(
    nodes
      .filter((n) => ['offline', 'error', 'skew'].includes(n.status) || n.manual_required)
      .map((n) => ({ node: n, text: n.manual_required ? '需要在节点上手动同步一次' : n.err || NODE_HINT[n.status] || '' })),
  );
  const linkIssues = $derived(links.filter((l) => linkStateGroup(l.state) === 'error' || l.state === 'waiting' || l.sync.retry));
  const issues = $derived(nodeIssues.length + linkIssues.length);
  const events = $derived(
    links
      .flatMap((link) => link.events.map((event) => ({ ...event, link })))
      .sort((a, b) => b.ts - a.ts)
      .slice(0, 10),
  );
</script>

<Page title="总览" subtitle={data ? `${issues ? `${issues} 项需要处理` : '运行正常'} · 每 5 秒刷新` : undefined}>
  {#if poll.error}<Alert tone="danger">{poll.error}</Alert>{/if}
  {#if !data}
    {#if poll.loading}<div class="flex justify-center py-24 text-fg-subtle"><Spinner class="size-5" /></div>{/if}
  {:else}
    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {#each tiles as tile (tile.page)}
        <a href={href(tile.page)} class="flex flex-col gap-1 rounded-lg border bg-surface p-4 shadow-sm transition-colors hover:border-border-strong">
          <span class="text-xs text-fg-muted">{tile.label}</span>
          <span class="tabular text-2xl leading-8 font-semibold">
            {tile.value}<span class="text-sm font-normal text-fg-subtle"> / {tile.total}</span>
          </span>
          <span class="text-xs {tile.total && tile.value < tile.total ? 'text-warn-text' : 'text-fg-muted'}">{tile.total ? tile.unit : '尚未添加'}</span>
        </a>
      {/each}
    </div>

    <Card title="需要处理" hint={issues ? `${issues} 项` : undefined} flush>
      {#if !issues}
        <EmptyState icon={CircleCheck} title="没有需要处理的问题" text={nodes.length ? '所有节点和链路都在正常运行。' : '先在「节点」页添加节点，再到「链路」页搭建转发。'} class="py-9" />
      {:else}
        <ul class="divide-y">
          {#each nodeIssues as { node, text } (node.id)}
            <li>
              <a href={href(`nodes/${node.id}`)} class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 hover:bg-surface-2">
                <span class="w-10 shrink-0 text-xs text-fg-subtle">节点</span>
                <span class="min-w-0 truncate text-sm font-medium">{node.name}</span>
                <NodeStatusBadge status={node.status} />
                <span class="min-w-0 flex-1 basis-40 truncate text-xs text-fg-muted" title={text}>{text}</span>
                <span class="tabular text-xs text-fg-subtle">{ago(node.last_seen, data.nodes.now)}</span>
              </a>
            </li>
          {/each}
          {#each linkIssues as link (link.id)}
            <li>
              <a href={href(`routes/${link.id}`)} class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 hover:bg-surface-2">
                <span class="w-10 shrink-0 text-xs text-fg-subtle">链路</span>
                <span class="min-w-0 truncate text-sm font-medium">{link.name}</span>
                <LinkStateBadge state={link.state} />
                <span class="min-w-0 flex-1 basis-40 truncate text-xs text-fg-muted" title={link.error}>{link.error || (link.sync.retry ? '同步失败，可在链路页重试' : '')}</span>
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>

    <div class="grid items-start gap-4 lg:grid-cols-2">
      <Card title="节点" hint="最近的连接状态" flush>
        {#if !nodes.length}
          <p class="px-4 py-6 text-center text-sm text-fg-muted">还没有节点</p>
        {:else}
          <ul class="divide-y">
            {#each nodes as node (node.id)}
              <li>
                <a href={href(`nodes/${node.id}`)} class="flex items-center gap-3 px-4 py-2 hover:bg-surface-2">
                  <span class="min-w-0 flex-1 truncate text-sm">{node.name}</span>
                  <StatusBar bar={node.bar} size="xs" class="hidden w-40 shrink-0 sm:block" />
                  <NodeStatusBadge status={node.status} class="w-24 shrink-0 justify-start" />
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      </Card>
      <Card title="最近事件" hint="来自各条链路" flush>
        {#if !events.length}
          <p class="px-4 py-6 text-center text-sm text-fg-muted">暂无事件</p>
        {:else}
          <ul class="divide-y">
            {#each events as event, i (i)}
              <li>
                <a href={href(`routes/${event.link.id}`)} class="flex items-baseline gap-3 px-4 py-2 hover:bg-surface-2">
                  <span class="tabular shrink-0 text-xs text-fg-subtle" title={new Date(event.ts * 1000).toLocaleString()}>{clock(event.ts)}</span>
                  <span class="min-w-0 flex-1 text-[0.8667rem] leading-snug">{event.message}</span>
                  <span class="max-w-28 shrink-0 truncate text-xs text-fg-muted">{event.link.name}</span>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      </Card>
    </div>
  {/if}
</Page>
