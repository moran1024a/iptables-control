<script lang="ts" module>
  import type { EntryStyle } from '$lib/topology/colors';

  export type CardBadge = { text: string; tone: 'accent' | 'warn' | 'danger' | 'idle' };
  /** How a card relates to the wire being drawn. */
  export type WireHint = { legal: boolean; reason: string | null; needsAddress: boolean };
  export type CardMark = EntryStyle & { name: string };
</script>

<script lang="ts">
  import Flag from '@lucide/svelte/icons/flag';
  import LogIn from '@lucide/svelte/icons/log-in';
  import Server from '@lucide/svelte/icons/server';
  import Zap from '@lucide/svelte/icons/zap';
  import { signed } from '$lib/format';
  import { NODE_STATUS } from '$lib/status';
  import { addressText, HEAD_HEIGHT, nodeOf, ROW_HEIGHT, vertexName, type AddressRow, type Box, type Catalog } from '$lib/topology/model';
  import { AUTO, type Vertex } from '$lib/types';
  import TagBadge from '$ui/TagBadge.svelte';

  type Props = {
    vertex: Vertex;
    box: Box;
    cat: Catalog;
    rows: AddressRow[];
    editing?: boolean;
    /** The header reacts to clicks. */
    interactive?: boolean;
    title?: string;
    /** Entries whose applied route passes through (view mode). */
    marks?: CardMark[];
    badge?: CardBadge | null;
    /** Addresses used by applied routes (view mode row labels). */
    used?: { in: Set<string>; out: Set<string> };
    onRoute?: boolean;
    subdued?: boolean;
    selected?: boolean;
    issue?: 'error' | 'warn' | null;
    emphasis?: 'option' | 'chosen' | 'failed' | null;
    /** Dashed outline colour of a failover candidate hop. */
    outline?: string | null;
    /** Highlighted connectors (edit mode). */
    boundIn?: string | null;
    boundOut?: string | null;
    wire?: WireHint | null;
    /** This card is where the wire being drawn starts. */
    wireSource?: string | null;
    dragging?: boolean;
    onactivate?: (event: MouseEvent) => void;
    ongrab?: (event: PointerEvent) => void;
    /** Pointer went down on an out connector. */
    onout?: (address: string, event: PointerEvent) => void;
    /** An IP row or in connector was chosen. */
    onin?: (address: string | null) => void;
    onhover?: (inside: boolean) => void;
  };
  let {
    vertex,
    box,
    cat,
    rows,
    editing = false,
    interactive = true,
    title,
    marks = [],
    badge = null,
    used,
    onRoute = false,
    subdued = false,
    selected = false,
    issue = null,
    emphasis = null,
    outline = null,
    boundIn = null,
    boundOut = null,
    wire = null,
    wireSource = null,
    dragging = false,
    onactivate,
    ongrab,
    onout,
    onin,
    onhover,
  }: Props = $props();

  const node = $derived(nodeOf(vertex, cat));
  const name = $derived(vertexName(vertex, cat));
  const direct = $derived(vertex.kind === 'direct');
  const role = $derived(
    vertex.runtime_only ? '旧路径' : vertex.kind === 'entry' ? '入口' : vertex.kind === 'target' ? '目标' : direct ? 'direct' : node?.mode === 'passive' ? '被动节点' : '节点',
  );
  const status = $derived(node ? NODE_STATUS[node.status] : null);
  const dots: Record<string, string> = { ok: 'bg-ok', warn: 'bg-warn', danger: 'bg-danger', info: 'bg-info', idle: 'bg-idle', accent: 'bg-accent' };
  // A resource without addresses still shows its main address.
  const shown = $derived<AddressRow[]>(
    rows.length ? rows : [{ id: '', name: '主 IP', host: (vertex.kind === 'target' ? cat.targets.get(vertex.target_id ?? -1)?.host : node?.addr) ?? '-' }],
  );
  const Icon = $derived(vertex.kind === 'entry' ? LogIn : vertex.kind === 'target' ? Flag : direct ? Zap : Server);

  const frame = $derived.by(() => {
    if (emphasis === 'failed') return 'border-danger ring-2 ring-danger';
    if (issue === 'error') return 'border-danger ring-2 ring-danger/60';
    if (selected) return 'border-accent ring-2 ring-accent/60';
    if (emphasis === 'option') return 'border-accent ring-4 ring-accent/20';
    if (emphasis === 'chosen') return 'border-warn ring-2 ring-warn/50';
    if (wire?.legal) return 'border-ok ring-2 ring-ok/30';
    if (issue === 'warn') return 'border-warn';
    if (onRoute) return 'border-border-strong';
    return 'border-border-strong/60';
  });
  const badgeTones = { accent: 'bg-accent text-accent-fg', warn: 'bg-warn-soft text-warn-text', danger: 'bg-danger-soft text-danger-text', idle: 'bg-surface-3 text-fg-muted' };
  const connector =
    'absolute top-1/2 z-10 flex size-4 -translate-y-1/2 items-center justify-center rounded-full border bg-surface transition-[transform,background-color,border-color] before:absolute before:-inset-1.5 before:content-[""] hover:scale-125';
  const rowLabel = (a: AddressRow) => a.name || (a.primary || a.id === 'main' || !a.id ? '主 IP' : 'IP');
</script>

<div
  data-card
  data-vertex={vertex.id}
  role="group"
  aria-label="{role} {name}"
  class="graph-card absolute border bg-surface text-left transition-[box-shadow,opacity,border-color] {direct ? 'rounded-full border-dashed' : 'rounded-lg'} {frame} {dragging
    ? 'z-30 shadow-lg'
    : selected
      ? 'z-20 shadow-md'
      : 'z-10 shadow-sm'} {subdued || (wire && !wire.legal) ? 'opacity-55' : ''} {vertex.runtime_only ? 'border-dashed' : ''}"
  style:left="{box.x}px"
  style:top="{box.y}px"
  style:width="{box.width}px"
  style:height="{box.height}px"
  style:outline={outline ? `2px dashed ${outline}` : undefined}
  style:outline-offset={outline ? '3px' : undefined}
  onpointerenter={(event) => event.pointerType !== 'touch' && onhover?.(true)}
  onpointerleave={(event) => event.pointerType !== 'touch' && onhover?.(false)}
  onfocusin={() => onhover?.(true)}
  onfocusout={() => onhover?.(false)}
>
  {#if vertex.kind === 'entry'}<span class="absolute inset-y-0 left-0 w-1 rounded-l-lg bg-accent"></span>{/if}
  {#if vertex.kind === 'target'}<span class="absolute inset-y-0 right-0 w-1 rounded-r-lg bg-ok"></span>{/if}

  {#if direct}
    {#snippet pill()}
      <Zap class="size-3.5 shrink-0 text-fg-subtle" />
      <span class="font-medium text-fg-muted">direct</span>
      <span class="tabular rounded-sm bg-surface-3 px-1 font-semibold" title="权重">{Number.isFinite(vertex.weight) ? signed(vertex.weight ?? 0) : '?'}</span>
    {/snippet}
    {#if interactive}
      <button
        type="button"
        data-nopan={editing ? '' : undefined}
        title={title ?? `direct · 权重 ${signed(vertex.weight ?? 0)}`}
        class="flex size-full items-center justify-center gap-1.5 rounded-full px-4 text-xs {editing ? 'cursor-grab touch-none' : 'cursor-pointer'}"
        onclick={onactivate}
        onpointerdown={ongrab}
      >
        {@render pill()}
      </button>
    {:else}
      <div class="flex size-full items-center justify-center gap-1.5 rounded-full px-4 text-xs" title={`direct · 权重 ${signed(vertex.weight ?? 0)}`}>{@render pill()}</div>
    {/if}
    {#if editing}
      <button
        type="button"
        data-nopan
        class="{connector} -left-2 {wire?.legal ? 'border-ok bg-ok-soft' : 'border-border-strong'}"
        aria-label="连入 direct"
        title={wire ? (wire.reason ?? '连接到此处') : '连入 direct'}
        onclick={() => onin?.(null)}
      ></button>
      <button
        type="button"
        data-nopan
        class="{connector} -right-2 cursor-crosshair touch-none {wireSource !== null ? 'border-accent bg-accent' : 'border-border-strong hover:border-accent'}"
        aria-label="连出 direct"
        title="从这里拖线到下一跳"
        onpointerdown={(event) => onout?.('', event)}
      ></button>
    {/if}
  {:else}
    {#snippet head()}
      <span class="flex h-[1.125rem] w-full items-center gap-1.5">
        <Icon class="size-3.5 shrink-0 {vertex.kind === 'entry' ? 'text-accent' : vertex.kind === 'target' ? 'text-ok' : 'text-fg-subtle'}" />
        <span class="shrink-0 text-2xs font-medium text-fg-muted">{role}</span>
        <span class="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
          {#each (node?.tags ?? []).slice(0, badge || marks.length ? 1 : 2) as tag (tag.id)}<TagBadge {tag} class="max-w-16 !shrink" />{/each}
        </span>
        {#if badge}
          <span class="shrink-0 rounded-full px-1.5 text-2xs leading-4 font-medium {badgeTones[badge.tone]}">{badge.text}</span>
        {:else}
          {#each marks as mark (mark.ordinal)}
            <span
              class="tabular flex size-4 shrink-0 items-center justify-center rounded-full text-[0.625rem] leading-none font-bold text-white"
              style:background={mark.color}
              title="{mark.name} 的当前线路经过这里"
            >{mark.ordinal}</span>
          {/each}
        {/if}
      </span>
      <span class="flex w-full items-center gap-1.5">
        {#if status}
          <span class="size-2 shrink-0 rounded-full {dots[status.tone]}" title="节点状态：{status.label}"></span>
        {/if}
        <strong class="min-w-0 flex-1 truncate text-sm leading-5 font-semibold" title={name}>{name}</strong>
        {#if vertex.kind !== 'target' && node}
          <span class="tabular shrink-0 rounded-sm bg-surface-3 px-1 text-2xs font-medium text-fg-muted" title="全局权重">{signed(node.weight)}</span>
        {/if}
      </span>
    {/snippet}
    {#if interactive}
      <button
        type="button"
        data-nopan={editing ? '' : undefined}
        {title}
        class="flex w-full flex-col justify-center gap-0.5 rounded-t-lg pr-2.5 pl-3 text-left {editing ? 'cursor-grab touch-none' : 'cursor-pointer hover:bg-surface-2'}"
        style:height="{HEAD_HEIGHT}px"
        onclick={onactivate}
        onpointerdown={ongrab}
      >
        {@render head()}
      </button>
    {:else}
      <div class="flex w-full flex-col justify-center gap-0.5 rounded-t-lg pr-2.5 pl-3 text-left" style:height="{HEAD_HEIGHT}px">{@render head()}</div>
    {/if}

    <div class="border-t">
      {#each shown as row (row.id)}
        {@const receiving = editing ? boundIn === row.id && !!row.id : used?.in.has(row.id)}
        {@const sending = editing ? boundOut === row.id && !!row.id : used?.out.has(row.id)}
        <div class="relative flex items-center" style:height="{ROW_HEIGHT}px">
          {#if editing}
            <button
              type="button"
              data-nopan
              class="flex size-full min-w-0 items-center gap-1.5 px-3 text-left text-2xs hover:bg-surface-2 {wire?.legal ? 'cursor-copy' : ''}"
              title={wire ? (wire.reason ?? '连接到此 IP') : vertex.kind === 'entry' ? '设为入口接收 IP' : addressText(row)}
              onclick={() => onin?.(row.id || null)}
            >
              <span class="w-9 shrink-0 truncate text-fg-muted">{rowLabel(row)}</span>
              <code class="min-w-0 flex-1 truncate">{addressText(row)}</code>
            </button>
            <button
              type="button"
              data-nopan
              tabindex="-1"
              class="{connector} -left-2 {wire?.legal ? 'border-ok bg-ok-soft' : receiving ? 'border-accent bg-accent' : 'border-border-strong'}"
              aria-label="{vertex.kind === 'entry' ? '入口接收 IP' : '接收'} {row.id || '主 IP'}"
              title={wire ? (wire.reason ?? '连接到此 IP') : vertex.kind === 'entry' ? '设为入口接收 IP' : '接收 IP：从其他卡片右侧拖线到这里'}
              onclick={() => onin?.(row.id || null)}
            ></button>
            {#if vertex.kind !== 'target'}
              <button
                type="button"
                data-nopan
                class="{connector} -right-2 cursor-crosshair touch-none {wireSource === row.id || sending ? 'border-accent bg-accent' : 'border-border-strong hover:border-accent'}"
                aria-label="发出 {row.id || '主 IP'}"
                title="发出 IP：从这里拖线到下一跳"
                disabled={!row.id}
                onpointerdown={(event) => onout?.(row.id, event)}
              ></button>
            {/if}
          {:else}
            <span class="flex size-full min-w-0 items-center gap-1.5 px-3 text-2xs">
              <span class="w-9 shrink-0 truncate text-fg-muted">{rowLabel(row)}</span>
              <code class="min-w-0 flex-1 truncate" title={addressText(row)}>{addressText(row)}</code>
              {#if receiving}<span class="shrink-0 rounded-sm bg-surface-3 px-1 font-medium text-fg-muted">入</span>{/if}
              {#if sending}<span class="shrink-0 rounded-sm bg-surface-3 px-1 font-medium text-fg-muted">出</span>{/if}
            </span>
          {/if}
        </div>
      {/each}
      {#if vertex.node_id != null}
        {@const sending = editing ? boundOut === AUTO : used?.out.has(AUTO)}
        <div class="relative flex items-center gap-1.5 px-3 text-2xs text-fg-muted" style:height="{ROW_HEIGHT}px">
          <span class="flex-1 truncate">自动出口</span>
          {#if !editing && sending}<span class="shrink-0 rounded-sm bg-surface-3 px-1 font-medium">出</span>{/if}
          {#if editing}
            <button
              type="button"
              data-nopan
              class="{connector} -right-2 cursor-crosshair touch-none {wireSource === AUTO || sending ? 'border-accent bg-accent' : 'border-border-strong hover:border-accent'}"
              aria-label="发出 自动出口"
              title="自动出口：从这里拖线到下一跳"
              onpointerdown={(event) => onout?.(AUTO, event)}
            ></button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>
