<script lang="ts">
  import ArrowUpDown from '@lucide/svelte/icons/arrow-up-down';
  import ClipboardPaste from '@lucide/svelte/icons/clipboard-paste';
  import FolderPlus from '@lucide/svelte/icons/folder-plus';
  import Link2 from '@lucide/svelte/icons/link-2';
  import Plus from '@lucide/svelte/icons/plus';
  import SearchX from '@lucide/svelte/icons/search-x';
  import { del, errorMessage, get } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { matches } from '$lib/format';
  import { anyFilter, clearFilters, filtersFor } from '$lib/lists.svelte';
  import { usePoll } from '$lib/poll.svelte';
  import { href } from '$lib/router.svelte';
  import { linkStateGroup } from '$lib/status';
  import { toast } from '$lib/toast.svelte';
  import type { ConfigsList, ExternalConfig, SubscriptionCandidate, SubscriptionsList, TopologyConfig } from '$lib/types';
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
  import ConfigRenameDialog from './ConfigRenameDialog.svelte';
  import ConfigRow from './ConfigRow.svelte';
  import ExternalConfigDialog from './ExternalConfigDialog.svelte';
  import { configKey, configSearchText, type AnyConfig } from './model';

  let sorting = $state(false);
  const list = usePoll((signal) => get<ConfigsList>('configs', { signal }), { interval: 5000, paused: () => sorting });
  // Subscriptions only say which configurations they include and which they cannot export; that rarely changes.
  const subscriptions = usePoll((signal) => get<SubscriptionsList>('subscriptions', { signal }), { interval: 30000 });
  const filters = filtersFor('configs', { q: '', state: '', source: '', group: '' });
  const filtering = $derived(anyFilter(filters));
  const data = $derived(list.data);
  const configs = $derived(data?.configs ?? []);
  const generated = $derived(configs.filter((config): config is TopologyConfig => config.source === 'topology'));
  const external = $derived(configs.filter((config): config is ExternalConfig => config.source === 'external'));
  const linkGroups = $derived((data?.groups ?? []).filter((group) => group.source === 'topology'));
  const externalGroups = $derived((data?.groups ?? []).filter((group) => group.source === 'external'));

  const visible = (config: AnyConfig) =>
    matches(configSearchText(config), filters.q) &&
    (!filters.state || linkStateGroup(config.state) === filters.state) &&
    (!filters.source || config.source === filters.source) &&
    (!filters.group || String(config.group_id) === filters.group);
  const shownGenerated = $derived(generated.filter(visible));
  const shownExternal = $derived(external.filter(visible));
  const shownCount = $derived(shownGenerated.length + shownExternal.length);
  const externalTotals = $derived(Object.fromEntries(externalGroups.map((g) => [g.id, external.filter((c) => c.group_id === g.id).length])));
  const canSort = $derived(external.length > 1 || externalGroups.length > 1);

  const candidates = $derived(new Map<string, SubscriptionCandidate>((subscriptions.data?.configs ?? []).map((item) => [configKey(item), item])));
  const usedBy = $derived.by(() => {
    const map = new Map<string, string[]>();
    for (const subscription of subscriptions.data?.subscriptions ?? []) {
      const keys = [...subscription.configuration_ids.map((id) => `C${id}`), ...subscription.external_configuration_ids.map((id) => `E${id}`)];
      for (const key of keys) map.set(key, [...(map.get(key) ?? []), subscription.name]);
    }
    return map;
  });

  let groupOpen = $state(false);
  let renameOpen = $state(false);
  let renameId = $state<number | null>(null);
  const renaming = $derived(generated.find((config) => config.id === renameId) ?? null);
  let externalOpen = $state(false);
  let externalId = $state<number | null>(null);
  const editing = $derived(external.find((config) => config.id === externalId) ?? null);

  function edit(config: AnyConfig) {
    if (config.source === 'topology') {
      renameId = config.id;
      renameOpen = true;
    } else {
      externalId = config.id;
      externalOpen = true;
    }
  }
  function importNew() {
    externalId = null;
    externalOpen = true;
  }

  async function remove(config: AnyConfig) {
    const used = usedBy.get(configKey(config)) ?? [];
    const ok = await confirm({
      title: '删除外部配置',
      body:
        `删除「${config.name}」（${config.code}）？` +
        (used.length ? `\n它会同时从这些订阅中移除：${used.join('、')}。` : subscriptions.data ? '\n没有订阅在使用它。' : '\n它也会从包含它的订阅中移除。'),
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`external-configs/${config.id}`);
      toast.ok('配置已删除');
    } catch (err) {
      toast.error(errorMessage(err));
    }
    await list.refresh();
  }
</script>

{#snippet heading(title: string, count: number, total: number, note: string)}
  <div class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-1">
    <h2 class="text-[0.8667rem] font-semibold">{title}</h2>
    <span class="tabular text-xs text-fg-subtle">{count !== total ? `${count} / ${total}` : total}</span>
    <span class="text-xs text-fg-muted">{note}</span>
  </div>
{/snippet}

<Page title="配置" subtitle="客户端实际填写的入口地址和分享链接，点击即可复制。">
  {#snippet meta()}
    {#if configs.length}
      <span class="tabular text-sm text-fg-subtle">{filtering ? `${shownCount} / ${configs.length}` : configs.length}</span>
    {/if}
  {/snippet}
  {#snippet actions()}
    {#if !sorting && data}
      <Button variant="primary" onclick={importNew}><ClipboardPaste />导入配置</Button>
      <Menu
        items={[
          { label: '新建外部配置分组', icon: FolderPlus, onselect: () => (groupOpen = true) },
          {
            label: '外部配置排序',
            icon: ArrowUpDown,
            disabled: !canSort || filtering,
            hint: filtering ? '筛选时不可用' : undefined,
            onselect: () => (sorting = true),
          },
        ]}
      />
    {/if}
  {/snippet}
  {#snippet toolbar()}
    {#if configs.length && !sorting && data}
      <SearchInput bind:value={filters.q} placeholder="搜索名称、编号或地址" class="w-56 max-sm:w-full" />
      <Select
        bind:value={filters.source}
        aria-label="来源"
        options={[{ value: '', label: '全部来源' }, { value: 'topology', label: '链路生成' }, { value: 'external', label: '外部导入' }]}
      />
      {#if generated.length && filters.source !== 'external'}
        <Select
          bind:value={filters.state}
          aria-label="链路配置的状态"
          options={[
            { value: '', label: '全部状态' },
            { value: 'active', label: '已生效' },
            { value: 'pending', label: '同步中' },
            { value: 'error', label: '异常' },
          ]}
        />
      {/if}
      {#if linkGroups.length > 1 || externalGroups.length > 1}
        <Select
          bind:value={filters.group}
          aria-label="分组"
          options={[
            { value: '', label: '全部分组' },
            ...linkGroups.map((g) => ({ value: String(g.id), label: `链路 · ${g.name}` })),
            ...externalGroups.map((g) => ({ value: String(g.id), label: `外部 · ${g.name}` })),
          ]}
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
    <p class="px-1 text-[0.8667rem] font-semibold">外部配置排序</p>
    <SortList
      kind="external_configurations"
      groups={externalGroups}
      items={external}
      onfinish={(saved) => {
        sorting = false;
        if (saved) void list.refresh();
      }}
    >
      {#snippet row(config)}
        <code class="shrink-0 rounded-sm bg-surface-3 px-1 text-2xs leading-4 font-medium text-fg-muted">{config.code}</code>
        <span class="truncate text-sm font-medium">{config.name}</span>
      {/snippet}
    </SortList>
  {:else if !configs.length && externalGroups.length <= 1}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState
        icon={Link2}
        title="还没有配置"
        text="配置是客户端连接用的地址或分享链接。保存链路后，每对入口和目标会自动生成一条；也可以把已有的分享链接导入进来，方便放进订阅。"
      >
        <Button variant="primary" href={href('routes/new')}><Plus />新建链路</Button>
        <Button onclick={importNew}><ClipboardPaste />导入配置</Button>
      </EmptyState>
    </div>
  {:else if filtering && !shownCount}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState icon={SearchX} title="没有匹配的配置">
        <Button onclick={() => clearFilters(filters)}>清除筛选</Button>
      </EmptyState>
    </div>
  {:else}
    {#if !filtering || shownGenerated.length}
      <section class="flex flex-col gap-2" aria-label="链路生成的配置">
        {@render heading('链路生成', shownGenerated.length, generated.length, '名称可以改；分组、顺序和增删跟随所属链路。')}
        {#if generated.length}
          <GroupedList kind="configs" label="配置" groups={linkGroups} items={shownGenerated} {filtering} editable={false} onchange={list.refresh}>
            {#snippet row(config)}
              <ConfigRow {config} candidate={candidates.get(configKey(config))} usedBy={usedBy.get(configKey(config))} onedit={edit} />
            {/snippet}
          </GroupedList>
        {:else}
          <p class="rounded-lg border border-dashed bg-surface px-4 py-4 text-center text-[0.8667rem] text-fg-muted">
            还没有链路生成的配置。<a class="font-medium text-accent-text hover:underline" href={href('routes')}>去链路页</a>保存一条链路后，每对入口和目标会在这里出现一条。
          </p>
        {/if}
      </section>
    {/if}
    {#if !filtering || shownExternal.length}
      <section class="flex flex-col gap-2" aria-label="外部导入的配置">
        {@render heading('外部导入', shownExternal.length, external.length, '导入的分享链接，不经过面板的节点，只用于统一放进订阅。')}
        {#if external.length || externalGroups.length > 1}
          <GroupedList
            kind="external_configurations"
            label="外部配置"
            groups={externalGroups}
            items={shownExternal}
            totals={externalTotals}
            {filtering}
            onchange={list.refresh}
          >
            {#snippet row(config)}
              <ConfigRow {config} candidate={candidates.get(configKey(config))} usedBy={usedBy.get(configKey(config))} onedit={edit} ondelete={remove} />
            {/snippet}
          </GroupedList>
        {:else}
          <p class="rounded-lg border border-dashed bg-surface px-4 py-4 text-center text-[0.8667rem] text-fg-muted">
            还没有外部配置。
            <button type="button" class="font-medium text-accent-text hover:underline" onclick={importNew}>导入一条分享链接</button>
          </p>
        {/if}
      </section>
    {/if}
  {/if}
</Page>

{#if data}
  <ConfigRenameDialog bind:open={renameOpen} config={renaming} onsaved={list.refresh} onrefresh={list.refresh} />
  <ExternalConfigDialog bind:open={externalOpen} config={editing} groups={externalGroups} existing={external} onsaved={list.refresh} onrefresh={list.refresh} />
{/if}
<GroupDialog bind:open={groupOpen} kind="external_configurations" label="外部配置" onsaved={list.refresh} />
