<script lang="ts">
  import { matches } from '$lib/format';
  import { href } from '$lib/router.svelte';
  import type { Group, SubscriptionCandidate } from '$lib/types';
  import SearchInput from '$ui/SearchInput.svelte';
  import { compatibility, configKey } from '../configs/model';
  import { unusable } from './model';

  type Props = {
    /** Everything selectable, in display order. */
    catalog: SubscriptionCandidate[];
    groups: (Group & { source: 'topology' | 'external' })[];
    /** Selected keys (`C<id>` / `E<id>`), kept in catalog order. */
    selected: string[];
    /** Keys selected when editing started: those stay toggleable even if nothing can export them any more. */
    locked?: string[];
  };
  let { catalog, groups, selected = $bindable(), locked = [] }: Props = $props();
  const uid = $props.id();

  let q = $state('');
  const chosen = $derived(new Set(selected));
  const keep = $derived(new Set(locked));
  const selectable = (item: SubscriptionCandidate) => !unusable(item) || keep.has(configKey(item));
  const visible = (item: SubscriptionCandidate) => matches(`${item.name}\n${item.code}`, q);

  const sections = $derived.by(() => {
    const several = { topology: groups.filter((g) => g.source === 'topology').length > 1, external: groups.filter((g) => g.source === 'external').length > 1 };
    return groups
      .map((group) => {
        const all = catalog.filter((item) => item.source === group.source && item.group_id === group.id);
        const source = group.source === 'topology' ? '链路生成' : '外部导入';
        return { key: `${group.source}-${group.id}`, title: several[group.source] ? `${source} · ${group.name}` : source, all, items: all.filter(visible) };
      })
      .filter((section) => section.all.length);
  });
  const shown = $derived(sections.flatMap((section) => section.items));

  function commit(next: Set<string>) {
    selected = catalog.map(configKey).filter((key) => next.has(key));
  }
  function toggle(item: SubscriptionCandidate, on: boolean) {
    const next = new Set(chosen);
    if (on) next.add(configKey(item));
    else next.delete(configKey(item));
    commit(next);
  }
  /** Tick state of a "select all" box over the given items. */
  function tally(items: SubscriptionCandidate[]) {
    const usable = items.filter(selectable);
    const on = usable.filter((item) => chosen.has(configKey(item))).length;
    return { usable: usable.length, all: usable.length > 0 && on === usable.length, some: on > 0 && on < usable.length };
  }
  function toggleAll(items: SubscriptionCandidate[], on: boolean) {
    const next = new Set(chosen);
    for (const item of items.filter(selectable)) {
      if (on) next.add(configKey(item));
      else next.delete(configKey(item));
    }
    commit(next);
  }
  const box = 'size-3.5 shrink-0 rounded-[4px] accent-(--accent)';
</script>

{#if !catalog.length}
  <p class="rounded-md border border-dashed px-3 py-4 text-center text-[0.8125rem] text-fg-muted">
    暂无配置，请先<a class="font-medium text-accent-text hover:underline" href={href('routes')}>创建链路</a>或<a
      class="font-medium text-accent-text hover:underline"
      href={href('configs')}>导入配置</a
    >。
  </p>
{:else}
  {@const overall = tally(shown)}
  <div class="flex flex-col gap-2">
    <div class="flex items-center gap-3">
      <!-- Enter in the search box must not submit the surrounding form. -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="min-w-0 flex-1" onkeydown={(event) => event.key === 'Enter' && event.preventDefault()}>
        <SearchInput bind:value={q} placeholder="搜索名称或编号" label="搜索配置" class="w-full" />
      </div>
      <label class="flex shrink-0 items-center gap-1.5 text-[0.8125rem] {overall.usable ? 'cursor-pointer' : 'opacity-60'}">
        <input
          type="checkbox"
          class={box}
          checked={overall.all}
          indeterminate={overall.some}
          disabled={!overall.usable}
          onchange={(event) => toggleAll(shown, event.currentTarget.checked)}
        />
        {q.trim() ? '全选搜索结果' : '全选'}
      </label>
    </div>
    <div class="max-h-[42vh] overflow-y-auto overscroll-contain rounded-md border">
      {#each sections as section (section.key)}
        {@const state = tally(section.items)}
        {#if section.items.length}
          <div role="group" aria-labelledby="{uid}-{section.key}">
            <div class="sticky top-0 z-10 flex h-8 items-center justify-between gap-2 border-b bg-surface-2 px-3">
              <span id="{uid}-{section.key}" class="truncate text-xs font-semibold text-fg-muted">
                {section.title}
                <span class="tabular ml-1 font-normal text-fg-subtle">{section.all.filter((item) => chosen.has(configKey(item))).length}/{section.all.length}</span>
              </span>
              <label class="flex shrink-0 items-center gap-1.5 text-xs text-fg-muted {state.usable ? 'cursor-pointer' : 'opacity-60'}">
                <input
                  type="checkbox"
                  class={box}
                  aria-label="全选 {section.title}"
                  checked={state.all}
                  indeterminate={state.some}
                  disabled={!state.usable}
                  onchange={(event) => toggleAll(section.items, event.currentTarget.checked)}
                />
                本组全选
              </label>
            </div>
            <ul class="divide-y">
              {#each section.items as item (configKey(item))}
                {@const compat = compatibility(item)}
                {@const disabled = !selectable(item)}
                <li>
                  <label class="flex items-start gap-2.5 px-3 py-1.5 {disabled ? 'cursor-not-allowed' : 'cursor-pointer hover:bg-surface-2'}">
                    <input
                      type="checkbox"
                      class="{box} mt-[3px]"
                      checked={chosen.has(configKey(item))}
                      {disabled}
                      onchange={(event) => toggle(item, event.currentTarget.checked)}
                    />
                    <span class="min-w-0 flex-1">
                      <span class="flex min-w-0 items-center gap-2 {disabled ? 'opacity-60' : ''}">
                        <span class="truncate text-sm">{item.name}</span>
                        <code class="shrink-0 text-2xs text-fg-subtle">{item.code}</code>
                      </span>
                      {#if compat.text}
                        <span class="block text-xs break-words {compat.tone === 'blocked' ? 'text-fg-muted' : 'text-warn-text'}">{compat.text}</span>
                      {/if}
                    </span>
                  </label>
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      {/each}
      {#if !shown.length}
        <p class="px-3 py-5 text-center text-[0.8125rem] text-fg-subtle">没有匹配的配置</p>
      {/if}
    </div>
    <p class="text-xs leading-relaxed text-fg-muted">
      已选 <b class="tabular font-semibold text-fg">{selected.length}</b> / {catalog.length}。订阅跟随配置自动更新；不兼容的配置只在对应格式中跳过，两种格式都不可用的不能新选。
    </p>
  </div>
{/if}
