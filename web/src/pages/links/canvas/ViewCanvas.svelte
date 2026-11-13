<script lang="ts">
  import { untrack } from 'svelte';
  import { ApiError, errorMessage, get, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { overlays } from '$lib/overlay.svelte';
  import { toast } from '$lib/toast.svelte';
  import { entryStyles } from '$lib/topology/colors';
  import { outgoing } from '$lib/topology/edit';
  import { addressesOf, boxesOf, catalogOf, layerLabels, vertexName, vertexOf, worldOf } from '$lib/topology/model';
  import { routeConnections, routeSpans } from '$lib/topology/routing';
  import { geometrySignature, stableBy } from '$lib/topology/stable';
  import { changedHops, entriesThrough, flowingHops, pathSummary, routesOf, spansOf, usedPorts, type RuntimeRoute } from '$lib/topology/runtime';
  import type { FailoverPreview, Graph, PreviewResult, Topology, TopologyResources, TopologySaved, VertexId } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import LinkStateBadge from '$ui/LinkStateBadge.svelte';
  import './canvas.css';
  import FailoverPanel, { type PreviewState } from './FailoverPanel.svelte';
  import GraphCard, { type CardBadge, type CardMark } from './GraphCard.svelte';
  import Viewport from './Viewport.svelte';
  import ZoomTools from './ZoomTools.svelte';

  type Props = {
    link: Topology;
    resources: TopologyResources;
    /** Another page-level request is running; canvas actions wait. */
    busy?: boolean;
    fullscreen: boolean;
    /** Paths were switched: reload the link. */
    onchanged: () => void;
  };
  let { link, resources, busy = false, fullscreen = $bindable(), onchanged }: Props = $props();

  type Plans = Extract<PreviewResult, { paths: unknown }>;
  interface Choice {
    entry: VertexId;
    revision: number;
    graph: Graph;
    prefix: VertexId[];
    options: VertexId[];
    plans: Plans['paths'];
  }

  // Poll results are new objects every few seconds; wires are only re-routed when the graph, the address
  // lists or the applied routes really change.
  const keepGraph = stableBy<Graph>();
  const keepGeometry = stableBy<Pick<TopologyResources, 'nodes' | 'targets'>>(geometrySignature);
  const keepRoutes = stableBy<RuntimeRoute[]>();

  const g = $derived(keepGraph(link.view_graph ?? link.graph));
  const cat = $derived(catalogOf(resources));
  const geometrySource = $derived(keepGeometry(resources));
  /** Catalog for geometry only: stays the same object while no address is added or removed. */
  const geo = $derived(catalogOf(geometrySource));
  const boxes = $derived(boxesOf(g, geo));
  const bounds = $derived(worldOf(boxes.values()));
  const styles = $derived(entryStyles(g));
  const labels = $derived(layerLabels(g));
  const deleting = $derived(link.state === 'deleting');

  let viewport = $state<Viewport>();
  let scale = $state(1);
  let working = $state(false);
  let choice = $state.raw<Choice | null>(null);
  let hovered = $state<VertexId | null>(null);
  let preview = $state.raw<PreviewState | null>(null);
  let coarse = $state(false);
  const locked = $derived(busy || working || deleting);

  const applied = $derived(keepRoutes(routesOf(link)));
  // While choosing hop by hop, the chosen prefix is drawn as a pending route.
  const routes = $derived<RuntimeRoute[]>(
    choice && choice.prefix.length > 1
      ? [...applied, { id: 'preview', entry: choice.entry, target: '', path: choice.prefix, graph: choice.graph, pending: true, state: 'pending' }]
      : applied,
  );
  const spanRoutes = $derived(routeSpans(g, spansOf(routes, g, [...styles.keys()]), boxes, geo));
  const wires = $derived(routeConnections(g, boxes, geo));
  const covered = $derived(new Set(routes.flatMap((r) => r.path.slice(1).map((b, i) => `${r.path[i]}/${b}`))));
  const flowing = $derived(flowingHops(applied, g, cat));
  const through = $derived(entriesThrough(applied));
  const used = $derived(usedPorts(applied, g));
  const onRoute = $derived(new Set(applied.flatMap((r) => r.path)));

  const nameOf = (id: VertexId | undefined) => vertexName(vertexOf(g, id), cat);
  const marksOf = (id: VertexId): CardMark[] =>
    (through.get(id) ?? []).flatMap((entry) => {
      const style = styles.get(entry);
      return style ? [{ ...style, name: nameOf(entry) }] : [];
    });
  const legend = $derived(g.vertices.filter((v) => v.kind === 'entry' && !v.runtime_only).map((v) => ({ id: v.id, name: nameOf(v.id), style: styles.get(v.id)! })));

  // Hover emphasis: routes through the hovered card stay, everything else fades.
  const focus = $derived.by(() => {
    if (!hovered || choice) return null;
    const keep = new Set<string>();
    for (const r of routes) if (r.path.includes(hovered)) for (let i = 1; i < r.path.length; i++) keep.add(`${r.entry}|${r.pending}|${r.path[i - 1]}|${r.path[i]}`);
    return keep;
  });

  // ---- path switching -------------------------------------------------------------------------------------------

  async function run<T>(action: () => Promise<T>): Promise<T | undefined> {
    if (working) return undefined;
    working = true;
    try {
      return await action();
    } catch (err) {
      toast.error(errorMessage(err));
      if (err instanceof ApiError && err.status === 409) {
        choice = null;
        onchanged();
      }
      return undefined;
    } finally {
      working = false;
    }
  }

  const planPreview = (body: { prefix: VertexId[]; required: VertexId[] }) =>
    post<Plans>('topology-preview', { id: link.id, graph: outgoing(link.graph), ...body });

  async function commit(graph: Graph, plans: Plans['paths'], revision: number) {
    if (!plans.length) {
      toast.info('没有可切换的配置');
      return;
    }
    const saved = await post<Extract<TopologySaved, { changed: number }>>(`topologies/${link.id}`, {
      graph: outgoing(graph),
      paths: plans.map((p) => p.path),
      name: link.name,
      group_id: link.group_id,
      revision,
      switch_only: true,
    });
    choice = null;
    toast.ok(saved.changed ? `正在切换 ${saved.changed} 项` : '无需切换');
    onchanged();
  }

  const planLines = (plans: Plans['paths'], graph: Graph) =>
    plans.map((p) => `${nameOf(p.entry)} → ${nameOf(p.target)}：${pathSummary(p.path, graph, cat).text}`).join('\n');

  /** Route every configuration that can pass through this relay or direct over it. */
  async function quickSwitch(id: VertexId) {
    if (locked) return;
    if (!vertexOf(link.graph, id)) {
      toast.info('已从设计中移除，等待旧路径清理');
      return;
    }
    const revision = link.revision;
    const result = await run(() => planPreview({ prefix: [], required: [id] }));
    if (!result) return;
    if (!result.paths.length) {
      toast.error('所选节点没有可达的入口—目标配置');
      return;
    }
    const changes = result.paths.filter((p) => !p.unchanged);
    if (!changes.length) {
      toast.info('无需切换');
      return;
    }
    closePreview();
    const ok = await confirm({
      title: '切换线路',
      body: `让以下 ${changes.length} 项配置改经「${nameOf(id)}」转发？节点确认后生效。`,
      detail: planLines(changes, result.graph),
      confirmLabel: '切换',
    });
    if (ok) await run(() => commit(result.graph, result.paths, revision));
  }

  async function advance(entry: VertexId, prefix: VertexId[], finish: boolean) {
    const revision = choice?.entry === entry ? choice.revision : link.revision;
    const result = await run(() => planPreview({ prefix, required: [] }));
    if (!result) return;
    choice = { entry, revision, graph: result.graph, prefix: result.prefix, options: result.options, plans: result.paths };
    if (finish || (result.complete && prefix.length > 1)) await run(() => commit(result.graph, result.paths, revision));
    else if (result.complete) {
      // Nothing to choose behind this entry: say so instead of switching on the first click.
      choice = null;
      toast.info(`「${nameOf(entry)}」之后没有可选的中转节点`);
    }
  }

  function activate(id: VertexId) {
    const v = vertexOf(g, id);
    if (!v || locked) return;
    if (choice) {
      if (v.kind === 'entry' && id !== choice.entry) void startChoice(id);
      else if (choice.options.includes(id)) void advance(choice.entry, [...choice.prefix, id], false);
      else if (choice.prefix.includes(id)) void advance(choice.entry, choice.prefix.slice(0, choice.prefix.indexOf(id) + 1), false);
      return;
    }
    if (v.kind === 'entry') void startChoice(id);
    else if (v.kind === 'node' && coarse && !v.runtime_only) openPreview(id, true);
    else if (v.kind === 'node' || v.kind === 'direct') void quickSwitch(id);
  }

  async function startChoice(entry: VertexId) {
    if (locked) return;
    if (!vertexOf(link.graph, entry)) {
      toast.info('已从设计中移除，等待旧路径清理');
      return;
    }
    closePreview();
    await advance(entry, [entry], false);
  }

  // A design change while choosing invalidates the session.
  $effect(() => {
    if (choice && (link.revision !== choice.revision || deleting)) {
      choice = null;
      toast.info('拓扑已变化，已退出选路');
    }
  });

  function badgeOf(id: VertexId): CardBadge | null {
    if (!choice) return null;
    if (choice.options.includes(id)) return { text: '选下一跳', tone: 'accent' };
    if (choice.prefix.includes(id)) return { text: '已选', tone: 'warn' };
    return null;
  }

  // ---- failover preview -----------------------------------------------------------------------------------------

  let openTimer: ReturnType<typeof setTimeout> | undefined;
  let closeTimer: ReturnType<typeof setTimeout> | undefined;
  let request = 0;

  function place(id: VertexId) {
    const box = boxes.get(id);
    const el = viewportEl;
    if (!box || !viewport || !el) return { left: 8, top: 8 };
    const width = 340;
    const right = viewport.toView(box.x + box.width, box.y);
    const left = viewport.toView(box.x, box.y);
    let x = right.x + 10;
    if (x + width > el.clientWidth - 8) x = left.x - width - 10;
    return { left: Math.max(8, Math.min(x, el.clientWidth - width - 8)), top: Math.max(8, Math.min(right.y, el.clientHeight - 260)) };
  }

  function openPreview(id: VertexId, pinned = false) {
    clearTimeout(closeTimer);
    if (preview?.vertex === id) return;
    preview = { vertex: id, pinned, loading: true, data: null, error: '', selected: 0, at: 0, ...place(id) };
    void loadPreview();
  }

  async function loadPreview() {
    const current = preview;
    if (!current) return;
    const mine = ++request;
    const revision = link.revision;
    preview = { ...current, loading: true };
    try {
      const data = await get<FailoverPreview>(`topologies/${link.id}/failover-preview?vertex=${encodeURIComponent(current.vertex)}&revision=${revision}`, { timeout: 5000 });
      if (mine !== request || !preview || data.revision !== untrack(() => link.revision)) return;
      preview = { ...preview, loading: false, data, error: '', selected: Math.min(preview.selected, Math.max(0, data.paths.length - 1)), at: Date.now() };
    } catch (err) {
      if (mine !== request || !preview) return;
      preview = { ...preview, loading: false, data: null, at: Date.now(), error: err instanceof ApiError && err.status === 409 ? '拓扑已变化，等待刷新' : errorMessage(err) };
    }
  }

  function closePreview() {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    request++;
    preview = null;
  }

  function hover(id: VertexId, inside: boolean) {
    if (inside) hovered = id;
    else if (hovered === id) hovered = null;
    if (coarse || preview?.pinned) return;
    clearTimeout(openTimer);
    const v = vertexOf(g, id);
    if (inside && v?.kind === 'node' && !v.runtime_only && !choice && !locked) {
      clearTimeout(closeTimer);
      if (preview?.vertex !== id) openTimer = setTimeout(() => openPreview(id), 250);
    } else if (!inside && preview) {
      clearTimeout(closeTimer);
      closeTimer = setTimeout(closePreview, 160);
    }
  }

  // Every poll delivers a new link object: refresh an open preview when it is older than 2 s.
  $effect(() => {
    void link;
    untrack(() => {
      if (preview && !preview.loading && Date.now() - preview.at > 2000) void loadPreview();
    });
  });
  $effect(() => {
    if (choice || locked) untrack(closePreview);
  });

  const candidate = $derived(preview?.data?.paths[preview.selected] ?? null);
  const candidateStyle = $derived(candidate ? styles.get(candidate.entry) : undefined);
  const candidateHops = $derived(candidate && preview?.data && candidate.path.length > 1 ? routeSpans(g, changedHops(candidate, preview.data.graph, applied, g), boxes, geo) : []);
  const candidateSet = $derived(new Set(candidate?.path ?? []));

  // ---- frame ----------------------------------------------------------------------------------------------------

  let viewportEl = $state<HTMLDivElement>();

  function keydown(event: KeyboardEvent) {
    if (event.key !== 'Escape' || overlays.open) return;
    if (choice) choice = null;
    else if (preview) closePreview();
    else if (fullscreen) fullscreen = false;
    else return;
    event.preventDefault();
  }
  function pointerdown(event: PointerEvent) {
    coarse = event.pointerType === 'touch';
    if (preview && !(event.target instanceof Element && event.target.closest('[data-failover]'))) {
      const card = event.target instanceof Element ? event.target.closest('[data-vertex]') : null;
      if (!card || card.getAttribute('data-vertex') !== preview.vertex) closePreview();
    }
  }
  const arrow = (points: [number, number][], height: number) => {
    const [x, y] = points[points.length - 1];
    const h = Math.max(3, height) / 2 + 1.2;
    return `M ${x - 7} ${y - h} L ${x} ${y} L ${x - 7} ${y + h} Z`;
  };
</script>

<svelte:window onkeydown={keydown} onpointerdowncapture={pointerdown} />

<div class="flex min-h-0 flex-1 flex-col">
  <div class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-3 py-2">
    {#if fullscreen}
      <strong class="max-w-48 truncate text-sm font-semibold">{link.name}</strong>
      <LinkStateBadge state={link.state} />
    {/if}
    {#if choice}
      <div class="flex min-w-0 flex-1 flex-wrap items-center gap-2" role="status">
        <span class="tabular flex size-5 shrink-0 items-center justify-center rounded-full text-2xs font-bold text-white" style:background={styles.get(choice.entry)?.color}>
          {styles.get(choice.entry)?.ordinal}
        </span>
        <p class="min-w-0 text-[0.8667rem]">
          <strong class="font-semibold">{choice.prefix.map(nameOf).join(' → ')}</strong>
          <span class="text-fg-muted"> · 点击下一跳{choice.options.length ? '' : '，或用自动补全完成'}</span>
        </p>
        <span class="flex-1"></span>
        <Button size="sm" onclick={() => (choice = null)} disabled={working}>取消选路</Button>
        <Button size="sm" variant="primary" loading={working} disabled={working} onclick={() => choice && advance(choice.entry, choice.prefix, true)} title="其余路段由系统按权重选择">
          自动补全
        </Button>
      </div>
    {:else}
      <div class="flex min-w-0 flex-1 flex-wrap items-center gap-1.5" aria-label="入口图例">
        {#each legend as item (item.id)}
          <button
            type="button"
            class="inline-flex h-7 max-w-56 items-center gap-1.5 rounded-full border bg-surface pr-2.5 pl-1 text-[0.8667rem] font-medium hover:bg-surface-2 disabled:opacity-60"
            title="从 {item.name} 选择线路"
            disabled={locked}
            onclick={() => startChoice(item.id)}
          >
            <span class="tabular flex size-5 shrink-0 items-center justify-center rounded-full text-2xs font-bold text-white" style:background={item.style.color}>{item.style.ordinal}</span>
            <span class="truncate">{item.name}</span>
          </button>
        {/each}
        <span class="ml-1 hidden items-center gap-3 text-xs text-fg-muted sm:flex">
          <span class="inline-flex items-center gap-1.5"><svg width="22" height="6" aria-hidden="true"><path d="M1 3 H21" class="wire" stroke="currentColor" stroke-width="2" /></svg>当前线路</span>
          <span class="inline-flex items-center gap-1.5"><svg width="22" height="6" aria-hidden="true"><path d="M1 3 H21" class="wire" stroke="currentColor" stroke-width="2" stroke-dasharray="5 4" /></svg>切换中</span>
          <span class="inline-flex items-center gap-1.5"><svg width="22" height="6" aria-hidden="true"><path d="M1 3 H21" class="wire" stroke="currentColor" stroke-width="1.2" stroke-dasharray="2 4" opacity="0.6" /></svg>备用连线</span>
        </span>
      </div>
    {/if}
    <ZoomTools {viewport} {scale} bind:fullscreen />
  </div>

  <Viewport bind:this={viewport} bind:el={viewportEl} bind:scale name={String(link.id)} {bounds} class="flex-1" onmove={() => !preview?.pinned && closePreview()}>
    <svg class="pointer-events-none absolute top-0 left-0 overflow-visible" width="1" height="1" aria-hidden="true">
      {#each wires as wire (wire.key)}
        {#if !covered.has(wire.key)}
          {@const near = hovered === wire.a || hovered === wire.b}
          <path
            d={wire.d}
            class="wire {near ? 'text-accent' : 'text-fg-subtle'}"
            stroke="currentColor"
            stroke-width="1.3"
            stroke-dasharray="2 5"
            opacity={near ? 0.95 : focus ? 0.25 : 0.6}
          />
        {/if}
      {/each}
      {#each spanRoutes as span (span.key)}
        {@const style = styles.get(span.entry)}
        {@const dim = choice ? span.entry !== choice.entry : focus ? !focus.has(`${span.entry}|${span.pending}|${span.a}|${span.b}`) : false}
        <g opacity={dim ? 0.22 : 1} class="transition-opacity" style:color={style?.color ?? 'var(--fg-muted)'}>
          <path d={span.d} class="wire-halo" stroke-width={span.stroke + span.halo} />
          <path d={span.d} class="wire" stroke="currentColor" stroke-width={span.stroke} stroke-dasharray={span.pending ? '5 5' : undefined} />
          <path d={arrow(span.points, span.arrowHeight)} fill="currentColor" />
          {#if !span.pending && flowing.has(`${span.entry}|${span.a}|${span.b}`)}
            <path d={span.d} class="wire-flow" stroke-width={Math.max(1, span.stroke - 0.6)} />
          {/if}
        </g>
      {/each}
      {#each candidateHops as hop (hop.key)}
        <g transform="translate(0 6)" style:color={candidateStyle?.color ?? 'var(--accent)'}>
          <path d={hop.d} class="wire-halo" stroke-width="6" />
          <path d={hop.d} class="wire" stroke="currentColor" stroke-width="2" stroke-dasharray="7 4" />
          <path d={arrow(hop.points, 6)} fill="currentColor" />
        </g>
      {/each}
    </svg>

    {#each labels as label (label.key)}
      <span class="absolute text-xs font-medium whitespace-nowrap text-fg-subtle" style:left="{label.x}px" style:top="{label.y}px">{label.text}</span>
    {/each}

    {#each g.vertices as vertex (vertex.id)}
      {@const id = vertex.id}
      {@const clickable = !deleting && vertex.kind !== 'target'}
      <GraphCard
        {vertex}
        {cat}
        box={boxes.get(id)!}
        rows={addressesOf(g, vertex, cat)}
        interactive={clickable}
        title={!clickable
          ? undefined
          : choice
            ? choice.options.includes(id)
              ? '选为下一跳'
              : choice.prefix.includes(id)
                ? '回退到这里'
                : undefined
            : vertex.kind === 'entry'
              ? `从 ${nameOf(id)} 逐跳选择线路`
              : vertex.runtime_only
                ? '已从设计中移除，等待旧路径清理'
                : `点击让线路改经 ${nameOf(id)}`}
        marks={marksOf(id)}
        badge={badgeOf(id)}
        used={used.get(id)}
        onRoute={onRoute.has(id)}
        subdued={choice ? !choice.options.includes(id) && !choice.prefix.includes(id) : !onRoute.has(id) && hovered !== id && !candidateSet.has(id)}
        emphasis={preview?.vertex === id && preview.data?.paths.length ? 'failed' : choice?.options.includes(id) ? 'option' : choice?.prefix.includes(id) ? 'chosen' : null}
        outline={candidate && candidateSet.has(id) && preview?.vertex !== id ? (candidateStyle?.color ?? null) : null}
        onactivate={() => activate(id)}
        onhover={(inside) => hover(id, inside)}
      />
    {/each}

    {#snippet overlay()}
      {#if !g.vertices.length}
        <p class="absolute inset-0 flex items-center justify-center text-sm text-fg-muted">这条链路还没有节点，点击「编辑」开始搭建。</p>
      {/if}
      {#if working}
        <div class="absolute top-2 left-1/2 -translate-x-1/2 rounded-full border bg-surface px-3 py-1 text-xs text-fg-muted shadow-sm" role="status">正在处理…</div>
      {/if}
      {#if preview}
        <FailoverPanel
          info={preview}
          name={nameOf(preview.vertex)}
          {styles}
          {nameOf}
          pathText={(path) => pathSummary(path, preview?.data?.graph ?? g, cat).full}
          sheet={coarse}
          canSwitch={preview.pinned && !locked}
          onselect={(index) => preview && (preview = { ...preview, selected: index })}
          onenter={() => clearTimeout(closeTimer)}
          onleave={() => !preview?.pinned && (closeTimer = setTimeout(closePreview, 160))}
          onclose={closePreview}
          onswitch={() => preview && quickSwitch(preview.vertex)}
        />
      {/if}
    {/snippet}
  </Viewport>
</div>
