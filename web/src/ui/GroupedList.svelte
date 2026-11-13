<script lang="ts" generics="T extends { id: number; group_id: number }">
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import type { Snippet } from 'svelte';
  import { del, errorMessage } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { collapsed } from '$lib/lists.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Group } from '$lib/types';
  import GroupDialog from './GroupDialog.svelte';

  type Props = {
    /** API kind used for groups, e.g. `nodes`. */
    kind: string;
    /** Display noun, e.g. `节点`. */
    label: string;
    groups: Group[];
    /** Items to show, already filtered and in display order. */
    items: T[];
    /** Unfiltered item count per group id, for the delete prompt. */
    totals?: Record<number, number>;
    /** A search or filter is active: empty groups hide and the rest open. */
    filtering?: boolean;
    /** Allow renaming and deleting groups. */
    editable?: boolean;
    row: Snippet<[T]>;
    /** Extra text after the item count in a group header. */
    summary?: Snippet<[T[]]>;
    onchange: () => void;
  };
  let { kind, label, groups, items, totals, filtering = false, editable = true, row, summary, onchange }: Props = $props();

  const byGroup = $derived.by(() => {
    const map = new Map<number, T[]>();
    for (const group of groups) map.set(group.id, []);
    for (const item of items) (map.get(item.group_id) ?? map.set(item.group_id, []).get(item.group_id)!).push(item);
    return map;
  });
  let renaming = $state<Group | null>(null);
  let renameOpen = $state(false);

  async function remove(group: Group) {
    const count = totals?.[group.id] ?? byGroup.get(group.id)?.length ?? 0;
    const fallback = groups.find((g) => g.builtin)?.name ?? '默认分组';
    const ok = await confirm({
      title: '删除分组',
      body: `确定删除分组「${group.name}」？` + (count ? `\n组内的 ${count} 个${label}将移到「${fallback}」。` : ''),
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`groups/${group.id}`);
      collapsed.forget(kind, group.id);
      toast.ok('分组已删除');
      onchange();
    } catch (err) {
      toast.error(errorMessage(err));
    }
  }
</script>

{#if groups.length <= 1}
  <div class="divide-y overflow-hidden rounded-lg border bg-surface shadow-sm">
    {#each items as item (item.id)}{@render row(item)}{/each}
  </div>
{:else}
  <div class="flex flex-col gap-3">
    {#each groups as group (group.id)}
      {@const members = byGroup.get(group.id) ?? []}
      {@const open = filtering || !collapsed.has(kind, group.id)}
      {#if !filtering || members.length}
        <section class="overflow-hidden rounded-lg border bg-surface shadow-sm">
          <header class="group/head flex h-10 items-center gap-1 bg-surface-2/70 pr-2 {open ? 'border-b' : ''}">
            <button
              type="button"
              aria-expanded={open}
              disabled={filtering}
              onclick={() => collapsed.toggle(kind, group.id)}
              class="flex h-full min-w-0 flex-1 items-center gap-2 pl-3 text-left disabled:cursor-default"
            >
              <ChevronDown class="size-4 shrink-0 text-fg-subtle transition-transform {open ? '' : '-rotate-90'}" />
              <span class="truncate text-[0.8125rem] font-semibold">{group.name}</span>
              <span class="shrink-0 text-xs text-fg-muted">{members.length} 个{#if summary}{@render summary(members)}{/if}</span>
            </button>
            {#if editable}
              <div class="flex gap-0.5 opacity-0 transition-opacity group-focus-within/head:opacity-100 group-hover/head:opacity-100 max-sm:opacity-100">
                <button
                  type="button"
                  title="改名"
                  aria-label="分组改名"
                  class="flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3 hover:text-fg"
                  onclick={() => {
                    renaming = group;
                    renameOpen = true;
                  }}
                >
                  <Pencil class="size-3.5" />
                </button>
                {#if !group.builtin}
                  <button
                    type="button"
                    title="删除分组"
                    aria-label="删除分组"
                    class="flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-danger-soft hover:text-danger-text"
                    onclick={() => remove(group)}
                  >
                    <Trash2 class="size-3.5" />
                  </button>
                {/if}
              </div>
            {/if}
          </header>
          {#if open}
            {#if members.length}
              <div class="divide-y">{#each members as item (item.id)}{@render row(item)}{/each}</div>
            {:else}
              <p class="px-4 py-5 text-center text-[0.8125rem] text-fg-subtle">空分组</p>
            {/if}
          {/if}
        </section>
      {/if}
    {/each}
  </div>
{/if}
<GroupDialog bind:open={renameOpen} {kind} {label} group={renaming} onsaved={onchange} />
