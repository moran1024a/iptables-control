<script lang="ts">
  import ArrowUpDown from '@lucide/svelte/icons/arrow-up-down';
  import Crosshair from '@lucide/svelte/icons/crosshair';
  import FolderPlus from '@lucide/svelte/icons/folder-plus';
  import Plus from '@lucide/svelte/icons/plus';
  import SearchX from '@lucide/svelte/icons/search-x';
  import { del, errorMessage, get } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { matches } from '$lib/format';
  import { anyFilter, clearFilters, filtersFor } from '$lib/lists.svelte';
  import { usePoll } from '$lib/poll.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Target, TargetsList } from '$lib/types';
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
  import Page from '../../layout/Page.svelte';
  import { typeLabel } from './target';
  import TargetForm from './TargetForm.svelte';
  import TargetRow from './TargetRow.svelte';

  let sorting = $state(false);
  const list = usePoll((signal) => get<TargetsList>('targets', { signal }), { interval: 5000, paused: () => sorting });
  const filters = filtersFor('targets', { q: '', group: '' });
  const filtering = $derived(anyFilter(filters));
  const data = $derived(list.data);
  const targets = $derived(data?.targets ?? []);
  const shown = $derived(
    targets.filter(
      (target) =>
        (matches(target.name, filters.q) || matches(`${target.host}:${target.port}`, filters.q)) &&
        (!filters.group || String(target.group_id) === filters.group),
    ),
  );
  const totals = $derived(Object.fromEntries((data?.groups ?? []).map((g) => [g.id, targets.filter((t) => t.group_id === g.id).length])));
  const canSort = $derived(targets.length > 1 || (data?.groups.length ?? 0) > 1);

  // A group picked in the filter may have been deleted since.
  $effect(() => {
    if (data && filters.group && !data.groups.some((group) => String(group.id) === filters.group)) filters.group = '';
  });

  let formOpen = $state(false);
  let editing = $state.raw<Target | null>(null);
  let groupOpen = $state(false);
  let deleting = $state<number | null>(null);

  function openForm(target: Target | null) {
    editing = target;
    formOpen = true;
  }

  async function remove(target: Target) {
    if (deleting !== null) return;
    if (target.configs.length) {
      toast.error(`目标「${target.name}」仍被链路中的配置「${target.configs.map((config) => config.remark).join('、')}」引用，请先解除引用`);
      return;
    }
    if (!(await confirm({ title: '删除目标', body: `确定删除目标「${target.name}」？`, confirmLabel: '删除', danger: true }))) return;
    deleting = target.id;
    try {
      await del(`targets/${target.id}`);
      toast.ok('已删除');
      await list.refresh();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      deleting = null;
    }
  }
</script>

<Page title="目标" subtitle="链路最终转发到的落地地址，可以是 IP／域名＋端口，或导入的 SS、VLESS 分享链接。">
  {#snippet meta()}
    {#if targets.length}
      <span class="tabular text-sm text-fg-subtle">{shown.length !== targets.length ? `${shown.length} / ${targets.length}` : targets.length}</span>
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if !sorting}
      {#if canSort}
        <Button onclick={() => (sorting = true)} disabled={filtering} title={filtering ? '搜索或筛选时不能排序' : undefined}>
          <ArrowUpDown />排序
        </Button>
      {/if}
      <Button variant="primary" onclick={() => openForm(null)} disabled={!data}><Plus />添加目标</Button>
      <Menu items={[{ label: '新建分组', icon: FolderPlus, onselect: () => (groupOpen = true) }]} />
    {/if}
  {/snippet}
  {#snippet toolbar()}
    {#if targets.length && !sorting && data}
      <SearchInput bind:value={filters.q} placeholder="搜索名称或地址" />
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
      kind="targets"
      groups={data.groups}
      items={targets}
      onfinish={(saved) => {
        sorting = false;
        if (saved) void list.refresh();
      }}
    >
      {#snippet row(target)}
        <span class="shrink-0 rounded-sm border px-1 text-2xs leading-4 font-medium text-fg-muted">{typeLabel(target)}</span>
        <span class="truncate text-sm font-medium">{target.name}</span>
        <code class="ml-auto min-w-0 truncate text-xs text-fg-muted">{target.host}:{target.port}</code>
      {/snippet}
    </SortList>
  {:else if !targets.length && data.groups.length <= 1}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState icon={Crosshair} title="还没有目标" text="添加一个落地地址，或导入 SS／VLESS 分享链接，之后就能在链路中选用。">
        <Button variant="primary" onclick={() => openForm(null)}><Plus />添加目标</Button>
      </EmptyState>
    </div>
  {:else if !shown.length && filtering}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState icon={SearchX} title="没有匹配的目标">
        <Button onclick={() => clearFilters(filters)}>清除筛选</Button>
      </EmptyState>
    </div>
  {:else}
    <GroupedList kind="targets" label="目标" groups={data.groups} items={shown} {totals} {filtering} onchange={list.refresh}>
      {#snippet row(target)}<TargetRow {target} busy={deleting === target.id} onedit={openForm} ondelete={remove} />{/snippet}
    </GroupedList>
  {/if}
</Page>

{#if data}
  <TargetForm bind:open={formOpen} target={editing} groups={data.groups} onsaved={list.refresh} />
{/if}
<GroupDialog bind:open={groupOpen} kind="targets" label="目标" onsaved={list.refresh} />
