<script lang="ts">
  import ArrowUpDown from '@lucide/svelte/icons/arrow-up-down';
  import FolderPlus from '@lucide/svelte/icons/folder-plus';
  import Plus from '@lucide/svelte/icons/plus';
  import SearchX from '@lucide/svelte/icons/search-x';
  import Server from '@lucide/svelte/icons/server';
  import Tags from '@lucide/svelte/icons/tags';
  import { get } from '$lib/api';
  import { matches } from '$lib/format';
  import { anyFilter, clearFilters, filtersFor } from '$lib/lists.svelte';
  import { usePoll } from '$lib/poll.svelte';
  import { router } from '$lib/router.svelte';
  import { NODE_STATUS, NODE_STATUS_ORDER } from '$lib/status';
  import type { NodesList } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import EmptyState from '$ui/EmptyState.svelte';
  import GroupDialog from '$ui/GroupDialog.svelte';
  import GroupedList from '$ui/GroupedList.svelte';
  import Menu from '$ui/Menu.svelte';
  import SearchInput from '$ui/SearchInput.svelte';
  import Select from '$ui/Select.svelte';
  import SortList from '$ui/SortList.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import TagList from '$ui/TagList.svelte';
  import Page from '../../layout/Page.svelte';
  import NodeForm from './NodeForm.svelte';
  import NodeRow from './NodeRow.svelte';
  import TagsManager from './TagsManager.svelte';

  let sorting = $state(false);
  const list = usePoll((signal) => get<NodesList>('nodes', { signal }), { interval: 5000, paused: () => sorting });
  const filters = filtersFor('nodes', { q: '', mode: '', status: '', group: '', tag: '' });
  const filtering = $derived(anyFilter(filters));
  const data = $derived(list.data);
  const nodes = $derived(data?.nodes ?? []);
  const shown = $derived(
    nodes.filter(
      (node) =>
        (matches(node.name, filters.q) || node.tags.some((tag) => matches(tag.name, filters.q))) &&
        (!filters.mode || node.desired_mode === filters.mode) &&
        (!filters.status || node.status === filters.status) &&
        (!filters.group || String(node.group_id) === filters.group) &&
        (!filters.tag || node.tags.some((tag) => String(tag.id) === filters.tag)),
    ),
  );
  const totals = $derived(Object.fromEntries((data?.groups ?? []).map((g) => [g.id, nodes.filter((n) => n.group_id === g.id).length])));
  const canSort = $derived(nodes.length > 1 || (data?.groups.length ?? 0) > 1);

  let formOpen = $state(false);
  let tagsOpen = $state(false);
  let groupOpen = $state(false);
</script>

<Page title="节点" subtitle="运行转发的服务器。入口和中转都在这里接入。">
  {#snippet meta()}
    {#if nodes.length}
      <span class="tabular text-sm text-fg-subtle">{shown.length !== nodes.length ? `${shown.length} / ${nodes.length}` : nodes.length}</span>
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if !sorting}
      {#if canSort}
        <Button onclick={() => (sorting = true)} disabled={filtering} title={filtering ? '搜索或筛选时不能排序' : undefined}>
          <ArrowUpDown />排序
        </Button>
      {/if}
      <Button onclick={() => (tagsOpen = true)}><Tags />标签</Button>
      <Button variant="primary" onclick={() => (formOpen = true)}><Plus />新增节点</Button>
      <Menu items={[{ label: '新建分组', icon: FolderPlus, onselect: () => (groupOpen = true) }]} />
    {/if}
  {/snippet}
  {#snippet toolbar()}
    {#if nodes.length && !sorting && data}
      <SearchInput bind:value={filters.q} placeholder="搜索名称或标签" />
      <Select
        bind:value={filters.mode}
        aria-label="同步模式"
        options={[{ value: '', label: '全部模式' }, { value: 'active', label: '主动' }, { value: 'passive', label: '被动' }]}
      />
      <Select
        bind:value={filters.status}
        aria-label="状态"
        options={[{ value: '', label: '全部状态' }, ...NODE_STATUS_ORDER.map((key) => ({ value: key as string, label: NODE_STATUS[key].label }))]}
      />
      {#if data.groups.length > 1}
        <Select
          bind:value={filters.group}
          aria-label="分组"
          options={[{ value: '', label: '全部分组' }, ...data.groups.map((g) => ({ value: String(g.id), label: g.name }))]}
        />
      {/if}
      {#if data.tags.length}
        <Select
          bind:value={filters.tag}
          aria-label="标签"
          options={[{ value: '', label: '全部标签' }, ...data.tags.map((t) => ({ value: String(t.id), label: t.name }))]}
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
      kind="nodes"
      groups={data.groups}
      items={nodes}
      onfinish={(saved) => {
        sorting = false;
        if (saved) void list.refresh();
      }}
    >
      {#snippet row(node)}
        <span class="truncate text-sm font-medium">{node.name}</span>
        <TagList tags={node.tags} expandable={false} />
        <code class="ml-auto shrink-0 text-xs text-fg-muted">{node.addr}</code>
      {/snippet}
    </SortList>
  {:else if !nodes.length && data.groups.length <= 1}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState icon={Server} title="还没有节点" text="新增节点后，在对应服务器执行一键接入命令即可完成安装。">
        <Button variant="primary" onclick={() => (formOpen = true)}><Plus />新增节点</Button>
      </EmptyState>
    </div>
  {:else if !shown.length && filtering}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState icon={SearchX} title="没有匹配的节点">
        <Button onclick={() => clearFilters(filters)}>清除筛选</Button>
      </EmptyState>
    </div>
  {:else}
    <GroupedList kind="nodes" label="节点" groups={data.groups} items={shown} {totals} {filtering} onchange={list.refresh}>
      {#snippet row(node)}<NodeRow {node} now={data.now} />{/snippet}
      {#snippet summary(members)}
        {@const offline = members.filter((node) => node.status === 'offline').length}
        {@const faulty = members.filter((node) => node.status === 'error' || node.status === 'skew').length}
        {#if offline}<span class="text-danger-text"> · {offline} 离线</span>{/if}
        {#if faulty}<span class="text-warn-text"> · {faulty} 异常</span>{/if}
      {/snippet}
    </GroupedList>
  {/if}
</Page>

{#if data}
  <NodeForm bind:open={formOpen} groups={data.groups} tags={data.tags} onsaved={(id) => router.go(`nodes/${id}`)} />
{/if}
<TagsManager bind:open={tagsOpen} onchange={list.refresh} />
<GroupDialog bind:open={groupOpen} kind="nodes" label="节点" onsaved={list.refresh} />
