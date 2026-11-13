<script lang="ts" generics="T extends { id: number; group_id: number }">
  import GripVertical from '@lucide/svelte/icons/grip-vertical';
  import type { Snippet } from 'svelte';
  import { flip } from 'svelte/animate';
  import { errorMessage, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { router } from '$lib/router.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Group } from '$lib/types';
  import Button from './Button.svelte';

  type Props = {
    /** API kind: the order is saved with `POST <kind>/order`. */
    kind: string;
    groups: Group[];
    items: T[];
    /** Compact content of one item. */
    row: Snippet<[T]>;
    /** Called when sort mode ends; `saved` tells whether to reload. */
    onfinish: (saved: boolean) => void;
  };
  let { kind, groups, items, row, onfinish }: Props = $props();

  type Section = { group: Group; items: T[] };
  // A private copy: the list must not shift under the pointer if the page data changes meanwhile.
  const serialise = (list: Section[]) => JSON.stringify(list.map((s) => [s.group.id, s.items.map((item) => item.id)]));
  // svelte-ignore state_referenced_locally
  const start: Section[] = groups.map((group) => ({ group, items: items.filter((item) => item.group_id === group.id) }));
  const initial = serialise(start);
  let sections = $state.raw<Section[]>(start);
  const changed = $derived(serialise(sections) !== initial);
  const grouped = $derived(sections.length > 1);
  let saving = $state(false);
  let dragging = $state<string | null>(null);

  $effect(() =>
    router.block(() =>
      changed ? { title: '放弃未保存的排序？', body: '此次顺序调整将不会保存。', confirmLabel: '放弃', danger: true } : null,
    ),
  );

  function move<V>(list: V[], from: number, to: number): V[] {
    if (to < 0 || to >= list.length || from === to) return list;
    const next = [...list];
    next.splice(to, 0, next.splice(from, 1)[0]);
    return next;
  }
  function moveGroup(index: number, to: number) {
    sections = move(sections, index, to);
  }
  function moveItem(sectionIndex: number, index: number, to: number) {
    const section = sections[sectionIndex];
    const next = move(section.items, index, to);
    if (next !== section.items) sections = sections.map((s, i) => (i === sectionIndex ? { ...s, items: next } : s));
  }

  /** Drag by the handle: the item swaps places whenever the pointer crosses a sibling's midpoint. */
  function startDrag(event: PointerEvent, key: string, selector: string, reorder: (from: number, to: number) => void) {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    event.preventDefault();
    const container = (event.currentTarget as HTMLElement).closest('[data-sort-container]') as HTMLElement;
    dragging = key;
    const onMove = (e: PointerEvent) => {
      const siblings = [...container.querySelectorAll<HTMLElement>(`:scope > ${selector}`)];
      const from = siblings.findIndex((el) => el.dataset.sortKey === key);
      let to = siblings.findIndex((el) => {
        const rect = el.getBoundingClientRect();
        return e.clientY < rect.top + rect.height / 2;
      });
      to = to === -1 ? siblings.length - 1 : to > from ? to - 1 : to;
      if (from !== -1 && to !== from) reorder(from, to);
      if (e.clientY < 60) window.scrollBy(0, -12);
      else if (e.clientY > window.innerHeight - 60) window.scrollBy(0, 12);
    };
    const onUp = () => {
      dragging = null;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
  }

  function keyMove(event: KeyboardEvent, index: number, reorder: (from: number, to: number) => void) {
    const step = event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0;
    if (!step) return;
    event.preventDefault();
    reorder(index, index + step);
    const handle = event.currentTarget as HTMLElement;
    requestAnimationFrame(() => handle.focus());
  }

  async function save() {
    saving = true;
    try {
      await post(`${kind}/order`, { groups: sections.map((s) => s.group.id), ids: sections.flatMap((s) => s.items.map((item) => item.id)) });
      toast.ok('顺序已保存');
      onfinish(true);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      saving = false;
    }
  }

  async function cancel() {
    if (saving) return;
    if (changed && !(await confirm({ title: '放弃未保存的排序？', body: '此次顺序调整将不会保存。', confirmLabel: '放弃', danger: true }))) return;
    onfinish(false);
  }
</script>

{#snippet handle(label: string, onpointerdown: (event: PointerEvent) => void, onkeydown: (event: KeyboardEvent) => void)}
  <button
    type="button"
    aria-label={label}
    title="拖动排序，或聚焦后按 ↑ ↓"
    {onpointerdown}
    {onkeydown}
    class="flex w-8 shrink-0 cursor-grab touch-none items-center self-stretch justify-center text-fg-subtle hover:text-fg active:cursor-grabbing"
  >
    <GripVertical class="size-4" />
  </button>
{/snippet}

<div class="sticky top-12 z-20 -mx-1 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-accent-soft px-3 py-2 lg:top-2" inert={saving}>
  <p class="text-[0.8667rem] text-accent-text">
    {grouped
      ? '拖动手柄调整分组和组内顺序，完成后保存。更换分组请在编辑中选择。'
      : '拖动左侧手柄调整顺序，完成后保存。'}
  </p>
  <div class="flex gap-2">
    <Button size="sm" onclick={cancel}>取消</Button>
    <Button size="sm" variant="primary" loading={saving} onclick={save}>完成</Button>
  </div>
</div>

<div class="flex flex-col gap-3" data-sort-container inert={saving}>
  {#each sections as section, sectionIndex (section.group.id)}
    <section
      data-sort-group
      data-sort-key="g{section.group.id}"
      animate:flip={{ duration: 160 }}
      class="overflow-hidden rounded-lg border bg-surface shadow-sm {dragging === 'g' + section.group.id ? 'relative z-10 border-accent shadow-md' : ''}"
    >
      {#if grouped}
        <header class="flex h-10 items-center border-b bg-surface-2/70 pr-3">
          {@render handle(
            `移动分组 ${section.group.name}`,
            (event) => startDrag(event, 'g' + section.group.id, '[data-sort-group]', moveGroup),
            (event) => keyMove(event, sectionIndex, moveGroup),
          )}
          <span class="truncate text-[0.8667rem] font-semibold">{section.group.name}</span>
          <span class="ml-2 text-xs text-fg-muted">{section.items.length} 个</span>
        </header>
      {/if}
      <div data-sort-container class="divide-y">
        {#each section.items as item, index (item.id)}
          <div
            data-sort-item
            data-sort-key="i{item.id}"
            animate:flip={{ duration: 160 }}
            class="flex min-h-10 items-stretch bg-surface {dragging === 'i' + item.id ? 'relative z-10 bg-accent-soft' : ''}"
          >
            {@render handle(
              '移动此项',
              (event) => startDrag(event, 'i' + item.id, '[data-sort-item]', (from, to) => moveItem(sectionIndex, from, to)),
              (event) => keyMove(event, index, (from, to) => moveItem(sectionIndex, from, to)),
            )}
            <div class="flex min-w-0 flex-1 items-center gap-2 py-1.5 pr-3">{@render row(item)}</div>
          </div>
        {:else}
          <p class="px-4 py-4 text-center text-[0.8667rem] text-fg-subtle">空分组</p>
        {/each}
      </div>
    </section>
  {/each}
</div>
