<script lang="ts" module>
  import type { FailoverPreview, VertexId } from '$lib/types';

  export interface PreviewState {
    vertex: VertexId;
    /** Opened by a tap: stays until dismissed. */
    pinned: boolean;
    loading: boolean;
    data: FailoverPreview | null;
    error: string;
    selected: number;
    /** Epoch ms of the last answer. */
    at: number;
    left: number;
    top: number;
  }
</script>

<script lang="ts">
  import X from '@lucide/svelte/icons/x';
  import { signed } from '$lib/format';
  import type { EntryStyle } from '$lib/topology/colors';
  import Button from '$ui/Button.svelte';
  import Spinner from '$ui/Spinner.svelte';

  type Props = {
    info: PreviewState;
    /** Name of the node assumed to fail. */
    name: string;
    styles: Map<VertexId, EntryStyle>;
    nameOf: (id: VertexId | undefined) => string;
    pathText: (path: VertexId[]) => string;
    /** Show as a bottom sheet (touch) instead of next to the card. */
    sheet?: boolean;
    canSwitch?: boolean;
    onselect: (index: number) => void;
    onenter: () => void;
    onleave: () => void;
    onclose: () => void;
    onswitch: () => void;
  };
  let { info: state, name, styles, nameOf, pathText, sheet = false, canSwitch = false, onselect, onenter, onleave, onclose, onswitch }: Props = $props();
  const rows = $derived(state.data?.paths ?? []);
</script>

<aside
  data-failover
  data-nopan
  data-scroll
  aria-label="灾备预览"
  class="absolute z-40 flex max-h-[min(22rem,80%)] cursor-default flex-col rounded-lg border bg-surface text-[0.8125rem] shadow-lg {sheet ? 'inset-x-2 bottom-2' : 'w-[340px]'}"
  style:left={sheet ? undefined : `${state.left}px`}
  style:top={sheet ? undefined : `${state.top}px`}
  onpointerenter={onenter}
  onpointerleave={onleave}
>
  <header class="flex items-center gap-2 border-b px-3 py-2">
    <div class="min-w-0 flex-1">
      <p class="text-2xs font-medium text-fg-muted">灾备预览</p>
      <p class="truncate font-semibold">{name} 失联</p>
    </div>
    {#if state.loading}<Spinner class="size-3.5 text-fg-subtle" />{/if}
    {#if state.pinned}
      <button type="button" class="flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3" aria-label="关闭" onclick={onclose}><X class="size-4" /></button>
    {/if}
  </header>
  <div class="min-h-0 flex-1 overflow-y-auto p-1.5">
    {#if state.error}
      <p class="px-1.5 py-2 text-danger-text" role="alert">{state.error}</p>
    {:else if !state.data}
      <p class="px-1.5 py-2 text-fg-muted">正在计算…</p>
    {:else if !rows.length}
      <p class="px-1.5 py-2 text-fg-muted">{state.data.message || '当前没有使用此节点的配置'}</p>
    {:else}
      {#each rows as row, index (row.id)}
        {@const style = styles.get(row.entry)}
        <button
          type="button"
          aria-pressed={state.selected === index}
          class="flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left hover:bg-surface-2 {state.selected === index ? 'bg-surface-2 ring-1 ring-border-strong' : ''}"
          onpointerenter={() => onselect(index)}
          onfocus={() => onselect(index)}
          onclick={() => onselect(index)}
        >
          <span class="flex items-center gap-1.5 font-medium">
            <span class="tabular flex size-4 shrink-0 items-center justify-center rounded-full text-[0.625rem] font-bold text-white" style:background={style?.color ?? 'var(--idle)'}>{style?.ordinal ?? ''}</span>
            <span class="truncate">
              {nameOf(row.entry)} → {nameOf(row.target)}{row.target_address && row.target_address.id !== 'main'
                ? ` · ${row.target_address.name || `${row.target_address.host}:${row.target_address.port}`}`
                : ''}
            </span>
          </span>
          {#if row.path.length}
            <span class="pl-5.5 text-xs break-words">{pathText(row.path)}</span>
            <span class="pl-5.5 text-xs text-fg-muted">从 {nameOf(row.switch_at)} 切换 · 权重 {signed(row.weight ?? 0)}</span>
          {:else}
            <span class="pl-5.5 text-xs {row.state === 'available' ? 'text-fg-muted' : 'text-warn-text'}">{row.message || '无可用备用路径'}</span>
          {/if}
        </button>
      {/each}
    {/if}
  </div>
  <footer class="flex items-center gap-2 border-t px-3 py-1.5 text-xs text-fg-subtle">
    <span class="min-w-0 flex-1 truncate">
      {#if state.loading && state.data}正在更新…{:else if rows.length}按当前状态预计 · {sheet ? '点选' : '指向'}条目查看路线{:else}仅预览，不会触发切换{/if}
    </span>
    {#if canSwitch}<Button size="xs" variant="primary" onclick={onswitch}>改经此节点</Button>{/if}
  </footer>
</aside>
