<script lang="ts">
  import ArrowUpDown from '@lucide/svelte/icons/arrow-up-down';
  import FolderPlus from '@lucide/svelte/icons/folder-plus';
  import Plus from '@lucide/svelte/icons/plus';
  import SearchX from '@lucide/svelte/icons/search-x';
  import Waypoints from '@lucide/svelte/icons/waypoints';
  import { get } from '$lib/api';
  import { matches } from '$lib/format';
  import { anyFilter, clearFilters, filtersFor } from '$lib/lists.svelte';
  import { usePoll } from '$lib/poll.svelte';
  import { href } from '$lib/router.svelte';
  import { linkStateGroup } from '$lib/status';
  import type { TopologiesList, Topology } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import EmptyState from '$ui/EmptyState.svelte';
  import GroupDialog from '$ui/GroupDialog.svelte';
  import GroupedList from '$ui/GroupedList.svelte';
  import LinkStateBadge from '$ui/LinkStateBadge.svelte';
  import Menu from '$ui/Menu.svelte';
  import SearchInput from '$ui/SearchInput.svelte';
  import Select from '$ui/Select.svelte';
  import SortList from '$ui/SortList.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Page from '../../layout/Page.svelte';
  import LinkListRow from './LinkListRow.svelte';

  let sorting = $state(false);
  const list = usePoll((signal) => get<TopologiesList>('topologies', { signal }), { interval: 5000, paused: () => sorting });
  const filters = filtersFor('topologies', { q: '', state: '', group: '' });
  const filtering = $derived(anyFilter(filters));
  const data = $derived(list.data);
  const links = $derived(data?.topologies ?? []);

  /** Name plus the names and addresses of every node and target the link passes through. */
  const searchText = $derived.by(() => {
    const nodes = new Map((data?.nodes ?? []).map((node) => [node.id, `${node.name} ${node.addr}`]));
    const targets = new Map((data?.targets ?? []).map((target) => [target.id, `${target.name} ${target.host}:${target.port}`]));
    return new Map(
      links.map((link) => [
        link.id,
        [
          link.name,
          ...link.graph.vertices.map((vertex) =>
            vertex.node_id !== undefined ? nodes.get(vertex.node_id) : vertex.target_id !== undefined ? targets.get(vertex.target_id) : '',
          ),
        ].join('\n'),
      ]),
    );
  });
  const shown = $derived(
    links.filter(
      (link) =>
        matches(searchText.get(link.id), filters.q) &&
        (!filters.state || linkStateGroup(link.state) === filters.state) &&
        (!filters.group || String(link.group_id) === filters.group),
    ),
  );
  const totals = $derived(Object.fromEntries((data?.groups ?? []).map((g) => [g.id, links.filter((l) => l.group_id === g.id).length])));
  const canSort = $derived(links.length > 1 || (data?.groups.length ?? 0) > 1);
  const summaryLine = (link: Topology) =>
    `${link.graph.vertices.filter((v) => v.kind === 'entry').length} 个入口 → ${link.graph.vertices.filter((v) => v.kind === 'target').length} 个目标 · ${link.configurations.length} 个配置`;

  let groupOpen = $state(false);
</script>

{#snippet empty()}
  <div class="rounded-lg border border-dashed bg-surface">
    {#if data && (!data.nodes.length || !data.targets.length)}
      <EmptyState
        icon={Waypoints}
        title="还没有链路"
        text="链路把入口节点经过中转连到目标，保存后为每对入口和目标生成一个可供客户端使用的配置。创建链路前需要先有{!data.nodes.length ? '节点' : ''}{!data.nodes.length && !data.targets.length ? '和' : ''}{!data.targets.length ? '目标' : ''}。"
      >
        {#if !data.nodes.length}<Button variant="primary" href={href('nodes')}>去新增节点</Button>{/if}
        {#if !data.targets.length}<Button variant={data.nodes.length ? 'primary' : 'secondary'} href={href('targets')}>去新增目标</Button>{/if}
      </EmptyState>
    {:else}
      <EmptyState
        icon={Waypoints}
        title="还没有链路"
        text="链路把入口节点经过中转连到目标，保存后为每对入口和目标生成一个可供客户端使用的配置。"
      >
        <Button variant="primary" href={href('routes/new')}><Plus />新建链路</Button>
      </EmptyState>
    {/if}
  </div>
{/snippet}

<Page title="链路" subtitle="入口到目标的转发路径。每对入口和目标生成一个配置。">
  {#snippet meta()}
    {#if links.length}
      <span class="tabular text-sm text-fg-subtle">{shown.length !== links.length ? `${shown.length} / ${links.length}` : links.length}</span>
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if !sorting}
      {#if canSort}
        <Button onclick={() => (sorting = true)} disabled={filtering} title={filtering ? '搜索或筛选时不能排序' : undefined}>
          <ArrowUpDown />排序
        </Button>
      {/if}
      <Button variant="primary" href={href('routes/new')}><Plus />新建链路</Button>
      <Menu items={[{ label: '新建分组', icon: FolderPlus, onselect: () => (groupOpen = true) }]} />
    {/if}
  {/snippet}
  {#snippet toolbar()}
    {#if links.length && !sorting && data}
      <SearchInput bind:value={filters.q} placeholder="搜索名称、节点或目标" class="w-56 max-sm:w-full" />
      <Select
        bind:value={filters.state}
        aria-label="状态"
        options={[
          { value: '', label: '全部状态' },
          { value: 'active', label: '已生效' },
          { value: 'pending', label: '同步中' },
          { value: 'error', label: '异常' },
          { value: 'deleting', label: '删除中' },
        ]}
      />
      {#if data.groups.length > 1}
        <Select
          bind:value={filters.group}
          aria-label="分组"
          options={[{ value: '', label: '全部分组' }, ...data.groups.map((g) => ({ value: String(g.id), label: g.name }))]}
        />
      {/if}
      {#if filtering}<Button variant="ghost" size="sm" onclick={() => clearFilters(filters)}>清除</Button>{/if}
    {/if}
  {/snippet}

  {#if list.error && data}
    <Alert tone="warn">刷新失败：{list.error}。显示的是上一次的数据。</Alert>
  {/if}
  {#if !data}
    {#if list.error}
      <Alert tone="danger">{list.error}</Alert>
    {:else}
      <div class="flex justify-center py-16 text-fg-subtle"><Spinner class="size-5" /></div>
    {/if}
  {:else if sorting}
    <SortList
      kind="topologies"
      groups={data.groups}
      items={links}
      onfinish={(saved) => {
        sorting = false;
        if (saved) void list.refresh();
      }}
    >
      {#snippet row(link)}
        <span class="truncate text-sm font-medium">{link.name}</span>
        <span class="truncate text-xs text-fg-muted max-sm:hidden">{summaryLine(link)}</span>
        <LinkStateBadge state={link.state} class="ml-auto" />
      {/snippet}
    </SortList>
  {:else if !shown.length && filtering && links.length}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState icon={SearchX} title="没有匹配的链路">
        <Button onclick={() => clearFilters(filters)}>清除筛选</Button>
      </EmptyState>
    </div>
  {:else}
    {#if !links.length}{@render empty()}{/if}
    {#if links.length || data.groups.length > 1}
      <GroupedList kind="topologies" label="链路" groups={data.groups} items={shown} {totals} {filtering} onchange={list.refresh}>
        {#snippet row(link)}<LinkListRow {link} onchange={list.refresh} />{/snippet}
        {#snippet summary(members)}
          {@const faulty = members.filter((link) => link.state === 'error').length}
          {@const syncing = members.filter((link) => linkStateGroup(link.state) === 'pending').length}
          {#if faulty}<span class="text-danger-text"> · {faulty} 异常</span>{/if}
          {#if syncing}<span class="text-warn-text"> · {syncing} 同步中</span>{/if}
        {/snippet}
      </GroupedList>
    {/if}
  {/if}
</Page>

<GroupDialog bind:open={groupOpen} kind="topologies" label="链路" onsaved={list.refresh} />
