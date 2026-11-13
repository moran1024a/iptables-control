<script lang="ts" module>
  export type LibraryKind = 'entry' | 'node' | 'target' | 'direct';
  export type LibraryItem = { kind: LibraryKind; id?: number };
  export const DRAG_TYPE = 'application/x-ictl-component';
</script>

<script lang="ts">
  import X from '@lucide/svelte/icons/x';
  import Zap from '@lucide/svelte/icons/zap';
  import { matches } from '$lib/format';
  import { NODE_STATUS } from '$lib/status';
  import type { Group, Graph, TopologyResources } from '$lib/types';
  import SearchInput from '$ui/SearchInput.svelte';
  import Segmented from '$ui/Segmented.svelte';
  import TagBadge from '$ui/TagBadge.svelte';

  type Props = {
    resources: TopologyResources;
    graph: Graph;
    kind: LibraryKind;
    disabled?: boolean;
    onadd: (item: LibraryItem) => void;
    onclose: () => void;
  };
  let { resources, graph, kind = $bindable(), disabled = false, onadd, onclose }: Props = $props();
  let query = $state('');

  const kinds: { value: LibraryKind; label: string }[] = [
    { value: 'entry', label: '入口' },
    { value: 'node', label: '节点' },
    { value: 'target', label: '目标' },
    { value: 'direct', label: 'direct' },
  ];
  const dots: Record<string, string> = { ok: 'bg-ok', warn: 'bg-warn', danger: 'bg-danger', info: 'bg-info', idle: 'bg-idle', accent: 'bg-accent' };

  interface Tile {
    id: number;
    name: string;
    address: string;
    group: number;
    tags: { id: number; name: string; color: string }[];
    dot?: string;
    status?: string;
    used: number;
  }
  const tiles = $derived.by<Tile[]>(() => {
    if (kind === 'direct') return [];
    if (kind === 'target')
      return resources.targets.map((t) => ({
        id: t.id,
        name: t.name,
        address: `${t.host}:${t.port}`,
        group: t.group_id,
        tags: [],
        used: graph.vertices.filter((v) => v.target_id === t.id).length,
      }));
    return resources.nodes.map((n) => ({
      id: n.id,
      name: n.name,
      address: n.addr,
      group: n.group_id,
      tags: n.tags,
      dot: dots[NODE_STATUS[n.status].tone],
      status: NODE_STATUS[n.status].label + (n.mode === 'passive' ? ' · 被动' : ''),
      used: graph.vertices.filter((v) => v.node_id === n.id).length,
    }));
  });
  const shown = $derived(tiles.filter((t) => matches(t.name, query) || matches(t.address, query) || t.tags.some((tag) => matches(tag.name, query))));
  const sections = $derived.by(() => {
    const groups: Group[] = kind === 'target' ? resources.target_groups : resources.node_groups;
    const known = new Set(groups.map((g) => g.id));
    const out = groups.map((g) => ({ id: g.id, name: g.name, items: shown.filter((t) => t.group === g.id) }));
    const rest = shown.filter((t) => !known.has(t.group));
    if (rest.length) out.push({ id: -1, name: '其他', items: rest });
    return out.filter((s) => s.items.length);
  });

  function dragstart(event: DragEvent, item: LibraryItem) {
    event.dataTransfer?.setData(DRAG_TYPE, JSON.stringify(item));
    event.dataTransfer?.setData('text/plain', item.kind);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copy';
  }
</script>

<div class="flex min-h-0 flex-1 flex-col" data-scroll>
  <div class="flex flex-col gap-2 border-b p-2.5">
    <div class="flex items-center gap-2">
      <h2 class="flex-1 text-[0.8125rem] font-semibold">资源库</h2>
      <button type="button" class="flex size-6 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3" aria-label="收起资源库" onclick={onclose}><X class="size-4" /></button>
    </div>
    <Segmented bind:value={kind} options={kinds} label="资源类型" class="w-full [&>button]:flex-1 [&>button]:justify-center [&>button]:px-1" />
    {#if kind !== 'direct'}<SearchInput bind:value={query} placeholder="搜索名称、地址或标签" label="搜索资源" class="w-full" />{/if}
  </div>
  <div class="min-h-0 flex-1 overflow-y-auto p-2">
    {#if kind === 'direct'}
      <button
        type="button"
        draggable="true"
        {disabled}
        class="flex w-full items-center gap-2 rounded-full border border-dashed border-border-strong bg-surface px-3 py-2 text-left text-[0.8125rem] hover:border-accent hover:bg-surface-2 disabled:opacity-50"
        ondragstart={(event) => dragstart(event, { kind: 'direct' })}
        onclick={() => onadd({ kind: 'direct' })}
      >
        <Zap class="size-3.5 text-fg-subtle" /><span class="font-medium">direct</span><span class="text-xs text-fg-muted">权重 0</span>
      </button>
      <p class="mt-2.5 px-1 text-xs leading-relaxed text-fg-muted">direct 放在一条连线上，只用来给这条线加减权重。也可以选中连线后点「＋ direct」直接插入。</p>
    {:else if !tiles.length}
      <p class="px-1 py-6 text-center text-xs text-fg-muted">{kind === 'target' ? '还没有目标，请先到「目标」页添加。' : '还没有节点，请先到「节点」页添加。'}</p>
    {:else if !shown.length}
      <p class="px-1 py-6 text-center text-xs text-fg-muted">没有匹配「{query}」的{kind === 'target' ? '目标' : '节点'}</p>
    {:else}
      {#each sections as section (section.id)}
        {#if sections.length > 1 || section.id === -1}<p class="px-1 pt-1.5 pb-1 text-2xs font-medium text-fg-subtle">{section.name}</p>{/if}
        <ul class="mb-1.5 flex flex-col gap-1">
          {#each section.items as tile (tile.id)}
            <li>
              <button
                type="button"
                draggable="true"
                {disabled}
                title="点击添加到画布中央，或拖到画布上"
                class="flex w-full flex-col gap-0.5 rounded-md border bg-surface px-2.5 py-1.5 text-left hover:border-accent hover:bg-surface-2 disabled:opacity-50"
                ondragstart={(event) => dragstart(event, { kind, id: tile.id })}
                onclick={() => onadd({ kind, id: tile.id })}
              >
                <span class="flex w-full items-center gap-1.5">
                  {#if tile.dot}<span class="size-2 shrink-0 rounded-full {tile.dot}" title={tile.status}></span>{/if}
                  <span class="min-w-0 flex-1 truncate text-[0.8125rem] font-medium">{tile.name}</span>
                  {#if tile.used}<span class="tabular shrink-0 text-2xs text-fg-subtle" title="已在画布上使用的次数">已用 {tile.used}</span>{/if}
                </span>
                <span class="flex w-full items-center gap-1 overflow-hidden">
                  <code class="min-w-0 flex-1 truncate text-2xs text-fg-muted">{tile.address}</code>
                  {#each tile.tags.slice(0, 2) as tag (tag.id)}<TagBadge {tag} class="max-w-16 !shrink" />{/each}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {/each}
    {/if}
  </div>
</div>
