<script lang="ts">
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import LayoutGrid from '@lucide/svelte/icons/layout-grid';
  import PanelLeftOpen from '@lucide/svelte/icons/panel-left-open';
  import Redo2 from '@lucide/svelte/icons/redo-2';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import Undo2 from '@lucide/svelte/icons/undo-2';
  import { untrack } from 'svelte';
  import { ApiError, errorMessage, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { snapshot } from '$lib/form.svelte';
  import { overlays } from '$lib/overlay.svelte';
  import { router } from '$lib/router.svelte';
  import { toast } from '$lib/toast.svelte';
  import { addVertex, connect, freePosition, insertDirect, moveVertex, newVertexId, outgoing, receiveAddress, removeEdge, removeVertex, setEntryAddress } from '$lib/topology/edit';
  import { amend, createHistory, record, redo, undo, type History } from '$lib/topology/history';
  import { addressesOf, boxesOf, catalogOf, emptyGraph, HEAD_HEIGHT, portAddress, portPoint, ROW_HEIGHT, vertexName, vertexOf, worldOf, type Edge, type Point } from '$lib/topology/model';
  import { elbow, routeConnections, type WireRoute } from '$lib/topology/routing';
  import { geometrySignature, stableBy } from '$lib/topology/stable';
  import { linkError, nameError, validate, type Issue } from '$lib/topology/rules';
  import { AUTO, type Graph, type PreviewResult, type Topology, type TopologyResources, type TopologySaved, type VertexId } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Input from '$ui/Input.svelte';
  import Select from '$ui/Select.svelte';
  import Tooltip from '$ui/Tooltip.svelte';
  import './canvas.css';
  import GraphCard, { type WireHint } from './GraphCard.svelte';
  import Inspector, { type Selection } from './Inspector.svelte';
  import Library, { DRAG_TYPE, type LibraryItem, type LibraryKind } from './Library.svelte';
  import Viewport from './Viewport.svelte';
  import ZoomTools from './ZoomTools.svelte';

  type Props = {
    /** The link being edited; null creates a new one. */
    link: Topology | undefined;
    resources: TopologyResources;
    fullscreen: boolean;
    onclose: () => void;
    onsaved: (id: number) => void;
    /** Fetch the newest server state (for "load latest" after a conflict). */
    latest: () => Promise<{ link?: Topology } | null>;
  };
  let { link, resources, fullscreen = $bindable(), onclose, onsaved, latest }: Props = $props();

  interface Doc {
    graph: Graph;
    name: string;
    group: number | null;
  }
  const docOf = (source: Topology | undefined): Doc =>
    source
      ? { graph: structuredClone(source.graph), name: source.name, group: source.group_id }
      : { graph: emptyGraph(), name: '', group: untrack(() => resources.groups[0]?.id ?? null) };

  // svelte-ignore state_referenced_locally
  let history = $state.raw<History<Doc>>(createHistory(docOf(link)));
  // svelte-ignore state_referenced_locally
  let revision = $state<number | undefined>(link?.revision);
  let baseline = $state(snapshot(untrack(() => history.present)));
  let coalesce: string | null = null;

  const doc = $derived(history.present);
  const dirty = $derived(snapshot(doc) !== baseline);
  const cat = $derived(catalogOf(resources));
  // Background refreshes replace `resources`; geometry only follows real address changes.
  const keepGeometry = stableBy<Pick<TopologyResources, 'nodes' | 'targets'>>(geometrySignature);
  const geometrySource = $derived(keepGeometry(resources));
  const geo = $derived(catalogOf(geometrySource));

  let viewport = $state<Viewport>();
  let scale = $state(1);
  // svelte-ignore state_referenced_locally
  let libraryOpen = $state(!link && window.innerWidth >= 640);
  // svelte-ignore state_referenced_locally
  let libraryKind = $state<LibraryKind>(link ? 'node' : 'entry');
  let selection = $state.raw<Selection>(null);
  let hovered = $state<VertexId | null>(null);
  let drag = $state.raw<{ id: VertexId; x: number; y: number } | null>(null);
  let wire = $state.raw<{ from: VertexId; out: string; pointer: Point | null; needs: VertexId | null } | null>(null);
  let flash = $state<{ text: string; tone: 'danger' | 'info' } | null>(null);
  let failure = $state<{ message: string; conflict: boolean } | null>(null);
  let serverIssue = $state.raw<Issue | null>(null);
  let nameInvalid = $state('');
  let saving = $state(false);
  let arranging = $state(false);
  let done = false;
  let issuesOpen = $state(false);
  let narrowNotice = $state(true);
  const busy = $derived(saving || arranging);

  // While a card is dragged its position lives outside the history; one undo step is written on release.
  const graph = $derived(drag ? moveVertex(doc.graph, drag.id, drag.x, drag.y) : doc.graph);
  const boxes = $derived(boxesOf(graph, geo));
  const bounds = $derived(worldOf(boxes.values()));
  let lastWires: WireRoute[] = [];
  const wires = $derived((lastWires = routeConnections(graph, boxes, geo, { editing: true, moving: drag?.id ?? null, previous: lastWires })));
  const issues = $derived<Issue[]>([...(serverIssue ? [serverIssue] : []), ...validate(doc.graph, cat)]);
  const errors = $derived(issues.filter((i) => i.level === 'error'));
  const warnings = $derived(issues.filter((i) => i.level === 'warn'));
  const stale = $derived(!!link && revision !== undefined && link.revision !== revision);
  const nameOf = (id: VertexId | undefined) => vertexName(vertexOf(doc.graph, id), cat);
  const edgeIs = (a: Edge | undefined | null, b: Edge) => !!a && a[0] === b[0] && a[1] === b[1];

  const vertexIssue = (id: VertexId): 'error' | 'warn' | null => {
    const mine = issues.filter((i) => i.vertex === id && (!i.edge || i.level === 'error'));
    return mine.some((i) => i.level === 'error') ? 'error' : mine.length ? 'warn' : null;
  };
  const wireHints = $derived.by(() => {
    const hints = new Map<VertexId, WireHint>();
    if (!wire) return hints;
    for (const v of doc.graph.vertices) {
      if (v.id === wire.from) continue;
      const reason = linkError(doc.graph, wire.from, v.id, cat);
      hints.set(v.id, { legal: !reason, reason, needsAddress: wire.needs === v.id });
    }
    return hints;
  });

  // ---- history --------------------------------------------------------------------------------------------------

  function say(text: string, tone: 'danger' | 'info' = 'danger') {
    flash = { text, tone };
  }
  function touched() {
    flash = null;
    serverIssue = null;
    if (failure && !failure.conflict) failure = null;
  }
  /** Record a new document state. Edits sharing a `key` (typing in one field) collapse into one undo step. */
  function commit(next: Partial<Doc>, key: string | null = null) {
    const value = { ...doc, ...next };
    history = key && key === coalesce ? amend(history, value) : record(history, value, snapshot);
    coalesce = key;
    touched();
  }
  const apply = (next: Graph, key?: string) => commit({ graph: next }, key ?? null);

  function step(direction: 'undo' | 'redo') {
    if (busy || drag) return;
    history = direction === 'undo' ? undo(history) : redo(history);
    coalesce = null;
    wire = null;
    selection = null;
    touched();
  }

  function reset(source: Topology | undefined) {
    history = createHistory(docOf(source));
    baseline = snapshot(history.present);
    revision = source?.revision;
    coalesce = null;
    selection = null;
    wire = null;
    drag = null;
    failure = null;
    serverIssue = null;
    flash = null;
    nameInvalid = '';
    queueMicrotask(() => viewport?.fit());
  }

  // Leaving with unsaved edits asks first (also covers closing the tab).
  $effect(() =>
    router.block(() => (dirty && !done ? { title: '离开拓扑？', body: '未保存的编辑将被丢弃。', confirmLabel: '离开', danger: true } : null)),
  );

  async function exit() {
    if (busy) return;
    if (dirty && !(await confirm({ title: '放弃当前编辑？', body: '未保存的修改将被丢弃。', confirmLabel: '放弃', danger: true }))) return;
    done = true;
    onclose();
  }

  async function reload() {
    if (busy) return;
    if (dirty && !(await confirm({ title: '载入最新配置？', body: '将放弃当前未保存的编辑，载入服务端最新状态。', confirmLabel: '载入', danger: true }))) return;
    arranging = true;
    const fresh = await latest();
    arranging = false;
    if (!fresh) {
      toast.error('载入失败，请稍后再试');
      return;
    }
    if (!fresh.link || fresh.link.state === 'deleting') {
      done = true;
      onclose();
      return;
    }
    reset(fresh.link);
    toast.ok('已载入最新配置');
  }

  // ---- selection and focus --------------------------------------------------------------------------------------

  function select(next: Selection) {
    selection = next;
    if (wire?.needs && next?.vertex !== wire.needs) wire = null;
  }
  function focusIssue(issue: Issue) {
    if (issue.edge && !issue.vertex) select({ edge: issue.edge });
    else if (issue.vertex) select({ vertex: issue.vertex });
    const box = boxes.get(issue.vertex ?? issue.edge?.[1] ?? '');
    if (box) viewport?.reveal(box, 60);
  }

  function fail(err: unknown) {
    if (!(err instanceof ApiError)) throw err;
    failure = { message: err.message, conflict: err.status === 409 };
    const field = err.field ?? '';
    if (field === 'name') nameInvalid = err.message;
    if (field.startsWith('vertex:')) {
      const id = field.slice(7);
      if (vertexOf(doc.graph, id)) {
        serverIssue = { level: 'error', message: err.message, vertex: id };
        focusIssue(serverIssue);
      }
    }
  }

  // ---- adding, removing -----------------------------------------------------------------------------------------

  function add(item: LibraryItem, at?: { x: number; y: number }) {
    if (busy || !viewport) return;
    const id = newVertexId();
    const centre = at ?? viewport.centre();
    apply(addVertex(doc.graph, cat, { id, kind: item.kind, resource: item.id, x: centre.x, y: centre.y, centre: !at }));
    select({ vertex: id });
    if (item.kind === 'entry') libraryKind = 'node';
    if (window.innerWidth < 640) libraryOpen = false;
    queueMicrotask(() => {
      const box = boxes.get(id);
      if (box) viewport?.reveal(box);
    });
  }

  function drop(event: DragEvent) {
    const raw = event.dataTransfer?.getData(DRAG_TYPE);
    if (!raw || !viewport) return;
    event.preventDefault();
    const point = viewport.toWorld(event.clientX, event.clientY);
    const item = JSON.parse(raw) as LibraryItem;
    const width = item.kind === 'direct' ? 110 : 220;
    add(item, freePosition(boxes.values(), point.x - width / 2, point.y - 40, width, 110));
  }

  function remove() {
    if (busy || !selection) return;
    if (selection.edge) apply(removeEdge(doc.graph, selection.edge));
    else {
      const result = removeVertex(doc.graph, selection.vertex);
      if (!result.graph) return say(result.error);
      apply(result.graph);
    }
    selection = null;
    wire = null;
  }

  function insert() {
    if (busy || !selection?.edge) return;
    const id = newVertexId();
    const result = insertDirect(doc.graph, cat, selection.edge, id);
    if (!result.graph) return say(result.error);
    apply(result.graph);
    select({ vertex: id });
  }

  // ---- dragging cards -------------------------------------------------------------------------------------------

  function grab(id: VertexId, event: PointerEvent) {
    if (event.button !== 0 || busy || wire) return;
    const v = vertexOf(doc.graph, id);
    if (!v || !viewport) return;
    const view = viewport;
    const start = view.toWorld(event.clientX, event.clientY);
    const offset = { x: start.x - v.x, y: start.y - v.y };
    const origin = { x: event.clientX, y: event.clientY };
    let pointer = origin;
    let frame = 0;
    const position = () => {
      const world = view.toWorld(pointer.x, pointer.y);
      drag = { id, x: Math.round(world.x - offset.x), y: Math.round(world.y - offset.y) };
    };
    // Near an edge the camera scrolls and the card keeps following the pointer.
    const tick = () => {
      if (!drag) return;
      const { dx, dy } = view.edgePan(pointer.x, pointer.y);
      if (dx || dy) position();
      frame = requestAnimationFrame(tick);
    };
    const move = (e: PointerEvent) => {
      pointer = { x: e.clientX, y: e.clientY };
      if (!drag && Math.hypot(pointer.x - origin.x, pointer.y - origin.y) < 4) return;
      const starting = !drag;
      position();
      if (starting) frame = requestAnimationFrame(tick);
    };
    const finish = (keep: boolean) => {
      cancelAnimationFrame(frame);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cancel);
      window.removeEventListener('blur', cancel);
      cancelDrag = null;
      const moved = drag;
      drag = null;
      if (keep && moved) apply(moveVertex(doc.graph, id, moved.x, moved.y));
    };
    const up = () => finish(true);
    const cancel = () => finish(false);
    cancelDrag = cancel;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cancel);
    window.addEventListener('blur', cancel);
  }
  let cancelDrag: (() => void) | null = null;

  // ---- wires ----------------------------------------------------------------------------------------------------

  /** Card and IP row under a world point. */
  function hit(x: number, y: number): { id: VertexId; address: string | null } | null {
    for (const v of [...doc.graph.vertices].reverse()) {
      const box = boxes.get(v.id)!;
      if (x < box.x - 10 || x > box.x + box.width + 4 || y < box.y || y > box.y + box.height) continue;
      const row = Math.floor((y - box.y - HEAD_HEIGHT) / ROW_HEIGHT);
      return { id: v.id, address: (row >= 0 && addressesOf(doc.graph, v, cat)[row]?.id) || null };
    }
    return null;
  }

  function startWire(id: VertexId, address: string, event: PointerEvent) {
    if (event.button !== 0 || busy || !viewport) return;
    event.preventDefault();
    const v = vertexOf(doc.graph, id);
    if (!v || v.kind === 'target') return;
    const out = v.kind === 'direct' ? '' : address;
    if (v.kind !== 'direct' && out !== AUTO && !addressesOf(doc.graph, v, cat).some((a) => a.id === out)) return say(`请先为「${nameOf(id)}」配置发出 IP`);
    // A second press on the connector of a pending wire cancels it.
    if (wire?.from === id && wire.out === out && !wire.pointer) {
      wire = null;
      return;
    }
    const view = viewport;
    const origin = { x: event.clientX, y: event.clientY };
    let pointer = origin;
    let moved = false;
    let frame = 0;
    wire = { from: id, out, pointer: null, needs: null };
    selection = null;
    flash = null;
    const track = () => {
      const world = view.toWorld(pointer.x, pointer.y);
      if (wire) wire = { ...wire, pointer: [world.x, world.y] };
    };
    const tick = () => {
      const { dx, dy } = view.edgePan(pointer.x, pointer.y);
      if (dx || dy) track();
      frame = requestAnimationFrame(tick);
    };
    const move = (e: PointerEvent) => {
      pointer = { x: e.clientX, y: e.clientY };
      if (!moved && Math.hypot(pointer.x - origin.x, pointer.y - origin.y) < 5) return;
      if (!moved) frame = requestAnimationFrame(tick);
      moved = true;
      track();
    };
    const finish = (released: boolean) => {
      cancelAnimationFrame(frame);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cancel);
      cancelWireDrag = null;
      if (!wire) return;
      if (!moved) {
        // A plain click leaves the wire pending: the next click on a card or IP completes it.
        wire = { ...wire, pointer: null };
        return;
      }
      const world = view.toWorld(pointer.x, pointer.y);
      const target = released ? hit(world.x, world.y) : null;
      wire = { ...wire, pointer: null };
      if (!released) wire = null;
      else if (!target || target.id === id) {
        wire = null;
        say('未连接到节点，请拖到目标节点或其 IP 行');
      } else finishWire(target.id, target.address);
    };
    const up = () => finish(true);
    const cancel = () => finish(false);
    cancelWireDrag = cancel;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cancel);
  }
  let cancelWireDrag: (() => void) | null = null;

  function finishWire(to: VertexId, address: string | null) {
    if (!wire) return;
    const error = linkError(doc.graph, wire.from, to, cat);
    if (error) return say(error);
    const target = vertexOf(doc.graph, to)!;
    let into: string | null = null;
    if (target.kind !== 'direct') {
      into = receiveAddress(doc.graph, to, cat, address);
      if (!into) {
        if (!addressesOf(doc.graph, target, cat).length) return say(`请先为「${nameOf(to)}」配置接收 IP`);
        wire = { ...wire, needs: to, pointer: null };
        selection = { vertex: to };
        return say(`请选择「${nameOf(to)}」的接收 IP`, 'info');
      }
    }
    const result = connect(doc.graph, cat, wire.from, to, wire.out || null, into);
    if (!result.graph) return say(result.error);
    const edge: Edge = [wire.from, to];
    wire = null;
    apply(result.graph);
    selection = { edge };
  }

  function activate(id: VertexId) {
    if (wire && wire.from !== id) finishWire(id, null);
    else if (!wire) select({ vertex: id });
  }
  function rowChosen(id: VertexId, address: string | null) {
    const v = vertexOf(doc.graph, id);
    if (!v || busy) return;
    if (wire && wire.from !== id) finishWire(id, address);
    else if (!wire && v.kind === 'entry' && address) {
      apply(setEntryAddress(doc.graph, id, address));
      select({ vertex: id });
    } else if (!wire) select({ vertex: id });
  }

  const wireStart = $derived.by<Point | null>(() => {
    if (!wire) return null;
    const v = vertexOf(graph, wire.from);
    const box = boxes.get(wire.from);
    if (!v || !box) return null;
    const [x, y] = portPoint(box, v, addressesOf(graph, v, cat), 'out', wire.out || null);
    return [x + 9, y];
  });

  // ---- arrange, save --------------------------------------------------------------------------------------------

  async function arrange() {
    if (busy || !doc.graph.vertices.length) return;
    arranging = true;
    try {
      const result = await post<PreviewResult>('topology-preview', { graph: outgoing(doc.graph), layout_only: true });
      wire = null;
      apply(result.graph);
      queueMicrotask(() => viewport?.fit());
    } catch (err) {
      fail(err);
    } finally {
      arranging = false;
    }
  }

  async function save() {
    if (busy) return;
    if (wire) return say('请先完成当前连线，或按 Esc 取消');
    nameInvalid = nameError(doc.name) ?? '';
    if (nameInvalid) return say(nameInvalid);
    // Only the client checks block a save; a problem reported by the server may be gone by now.
    const blocking = errors.filter((i) => i !== serverIssue);
    if (blocking.length) {
      issuesOpen = true;
      focusIssue(blocking[0]);
      return say(`还有 ${blocking.length} 个问题需要处理：${blocking[0].message}`);
    }
    saving = true;
    try {
      const result = await post<Extract<TopologySaved, { configurations: number }>>(link ? `topologies/${link.id}` : 'topologies', {
        name: doc.name.trim(),
        group_id: doc.group,
        graph: outgoing(doc.graph),
        revision,
      });
      done = true;
      toast.ok(`已保存 · ${result.configurations} 个配置${result.warnings.length ? ' · 部分节点未形成完整路径' : ''}`);
      onsaved(result.id);
    } catch (err) {
      try {
        fail(err);
      } catch (other) {
        toast.error(errorMessage(other));
      }
    } finally {
      saving = false;
    }
  }

  // ---- keyboard -------------------------------------------------------------------------------------------------

  function keydown(event: KeyboardEvent) {
    if (overlays.open) return;
    const typing = event.target instanceof HTMLElement && !!event.target.closest('input, select, textarea, [contenteditable]');
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (event.key === 'Escape') {
      if (cancelDrag) cancelDrag();
      else if (cancelWireDrag) cancelWireDrag();
      else if (wire) wire = null;
      else if (selection) selection = null;
      else if (fullscreen) fullscreen = false;
      else return;
    } else if (typing) return;
    else if (mod && key === 'z') step(event.shiftKey ? 'redo' : 'undo');
    else if (mod && key === 'y') step('redo');
    else if (mod && key === 's') void save();
    else if ((event.key === 'Delete' || event.key === 'Backspace') && selection && !mod) remove();
    else return;
    event.preventDefault();
  }

  const groupOptions = $derived(resources.groups.map((g) => ({ value: g.id, label: g.name })));
  const tool = 'flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3 hover:text-fg disabled:opacity-40 disabled:hover:bg-transparent [&_svg]:size-4';
  const arrow = (points: Point[]) => {
    const [x, y] = points[points.length - 1];
    return `M ${x - 7} ${y - 3.5} L ${x} ${y} L ${x - 7} ${y + 3.5} Z`;
  };
  const hint = $derived(
    wire?.needs ? '选择接收 IP 完成连线' : wire ? '点击目标卡片或其左侧 IP 点完成连线 · Esc 取消' : '从卡片右侧的连接点拖线到下一跳',
  );
</script>

<svelte:window onkeydown={keydown} />

<div class="flex min-h-0 flex-1 flex-col">
  <div class="flex flex-wrap items-center gap-2 border-b px-3 py-2">
    <Input
      value={doc.name}
      maxlength={64}
      placeholder="链路名称"
      aria-label="链路名称"
      invalid={!!nameInvalid}
      title={nameInvalid || undefined}
      disabled={busy}
      class="!w-44 sm:!w-56"
      oninput={(e) => {
        nameInvalid = '';
        commit({ name: e.currentTarget.value }, 'name');
      }}
    />
    {#if groupOptions.length > 1 || (doc.group !== null && !groupOptions.some((g) => g.value === doc.group))}
      <Select value={doc.group ?? groupOptions[0]?.value} options={groupOptions} aria-label="链路分组" disabled={busy} class="max-w-40" onchange={(e) => commit({ group: Number(e.currentTarget.value) })} />
    {/if}
    <span class="flex-1"></span>
    {#if dirty}<span class="hidden text-xs text-fg-muted sm:inline">有未保存的修改</span>{/if}
    <Button onclick={exit} disabled={busy}>取消</Button>
    <Button variant="primary" loading={saving} disabled={busy} onclick={save} title="保存并开始同步到节点 (Ctrl/⌘ S)">保存</Button>
  </div>

  <div class="flex flex-wrap items-center gap-x-2 gap-y-1.5 border-b bg-surface-2/60 px-3 py-1.5">
    <Button size="sm" variant={libraryOpen ? 'secondary' : 'primary'} aria-pressed={libraryOpen} onclick={() => (libraryOpen = !libraryOpen)}><PanelLeftOpen />添加</Button>
    <div class="flex items-center gap-0.5 rounded-lg border bg-surface p-0.5 shadow-sm">
      <Tooltip text="撤销 Ctrl/⌘ Z"><button type="button" class={tool} aria-label="撤销" disabled={!history.past.length || busy} onclick={() => step('undo')}><Undo2 /></button></Tooltip>
      <Tooltip text="重做 Ctrl/⌘ Shift Z"><button type="button" class={tool} aria-label="重做" disabled={!history.future.length || busy} onclick={() => step('redo')}><Redo2 /></button></Tooltip>
    </div>
    <Tooltip text="由服务端检查结构并自动排版（可撤销）">
      <Button size="sm" loading={arranging} disabled={busy || !doc.graph.vertices.length} onclick={arrange}><LayoutGrid />整理</Button>
    </Tooltip>
    <p class="hidden min-w-0 flex-1 truncate text-xs text-fg-muted md:block" aria-live="polite">{hint}</p>
    <span class="flex-1 md:hidden"></span>
    <ZoomTools {viewport} {scale} bind:fullscreen />
  </div>

  {#if failure || stale}
    <div class="border-b p-2">
      <Alert tone="danger" title={failure?.conflict || (stale && !failure) ? '服务端的配置已经变化' : '保存失败'}>
        {failure?.message ?? '这条链路在你编辑期间被修改（其他人保存，或自动灾备切换了路径），现在保存会被拒绝。'}
        {#snippet actions()}
          {#if link && (failure?.conflict || stale)}<Button size="sm" loading={arranging} disabled={busy} onclick={reload}>载入最新配置</Button>{/if}
        {/snippet}
      </Alert>
    </div>
  {/if}

  <div class="relative flex min-h-0 flex-1">
    {#if libraryOpen}
      <aside class="absolute inset-x-0 bottom-0 z-40 flex max-h-[62%] flex-col rounded-t-xl border-t bg-surface shadow-lg sm:static sm:max-h-none sm:w-60 sm:shrink-0 sm:rounded-none sm:border-t-0 sm:border-r sm:shadow-none" aria-label="资源库">
        <Library {resources} graph={doc.graph} bind:kind={libraryKind} disabled={busy} onadd={(item) => add(item)} onclose={() => (libraryOpen = false)} />
      </aside>
    {/if}

    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="flex min-w-0 flex-1" ondragover={(e) => e.dataTransfer?.types.includes(DRAG_TYPE) && e.preventDefault()} ondrop={drop}>
      <Viewport bind:this={viewport} bind:scale name={link ? `edit-${link.id}` : 'new'} {bounds} autofit={false} label="拓扑编辑画布" class="flex-1" onbackground={() => (wire ? (wire = null) : (selection = null))}>
        <svg class="absolute top-0 left-0 overflow-visible" width="1" height="1">
          {#each wires as route (route.key)}
            {@const edge: Edge = [route.a, route.b]}
            {@const chosen = edgeIs(selection?.edge, edge)}
            {@const broken = errors.some((i) => edgeIs(i.edge, edge))}
            {@const related = hovered === route.a || hovered === route.b || selection?.vertex === route.a || selection?.vertex === route.b}
            {@const faded = !chosen && !related && !!(hovered || selection)}
            <g class={chosen ? 'text-accent' : broken ? 'text-danger' : related ? 'text-accent' : 'text-fg-muted'} opacity={faded ? 0.3 : 1}>
              <path d={route.d} class="wire-halo pointer-events-none" stroke-width="5" />
              <path d={route.d} class="wire pointer-events-none" stroke="currentColor" stroke-width={chosen ? 2.2 : 1.5} stroke-dasharray={broken && !chosen ? '4 3' : undefined} />
              <path d={arrow(route.points)} fill="currentColor" class="pointer-events-none" />
              <path
                d={route.d}
                data-nopan
                data-edge={route.key}
                role="button"
                tabindex="0"
                aria-label="连线 {nameOf(route.a)} → {nameOf(route.b)}"
                aria-pressed={chosen}
                class="wire cursor-pointer outline-none focus-visible:stroke-accent/30"
                stroke="transparent"
                stroke-width="14"
                pointer-events="stroke"
                onclick={() => select({ edge })}
                onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), select({ edge }))}
              >
                <title>{nameOf(route.a)} → {nameOf(route.b)}</title>
              </path>
            </g>
          {/each}
          {#if wire?.pointer && wireStart}
            <path d={elbow(wireStart, wire.pointer)} class="wire pointer-events-none text-accent" stroke="currentColor" stroke-width="2" stroke-dasharray="6 4" />
          {/if}
        </svg>

        {#each graph.vertices as vertex (vertex.id)}
          {@const id = vertex.id}
          {@const edge = selection?.edge}
          <GraphCard
            {vertex}
            {cat}
            editing
            box={boxes.get(id)!}
            rows={addressesOf(graph, vertex, cat)}
            selected={selection?.vertex === id}
            issue={vertexIssue(id)}
            badge={wire?.needs === id ? { text: '选择接收 IP', tone: 'accent' } : vertex.kind === 'entry' && !vertex.address && addressesOf(graph, vertex, cat).length !== 1 ? { text: '选择入口 IP', tone: 'danger' } : null}
            boundIn={edge ? (edge[1] === id ? portAddress(graph, vertex, 'in', edge, cat) : null) : portAddress(graph, vertex, 'in', null, cat)}
            boundOut={edge ? (edge[0] === id ? portAddress(graph, vertex, 'out', edge, cat) : null) : vertex.kind === 'target' ? null : portAddress(graph, vertex, 'out', null, cat)}
            wire={wireHints.get(id) ?? null}
            wireSource={wire?.from === id ? wire.out : null}
            dragging={drag?.id === id}
            onactivate={() => activate(id)}
            ongrab={(event) => {
              if (!wire) select({ vertex: id });
              grab(id, event);
            }}
            onout={(address, event) => startWire(id, address, event)}
            onin={(address) => rowChosen(id, address)}
            onhover={(inside) => (hovered = inside ? id : hovered === id ? null : hovered)}
          />
        {/each}

        {#snippet overlay()}
          {#if !doc.graph.vertices.length}
            <div class="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-1.5 px-6 text-center">
              <p class="text-sm font-medium">从资源库添加入口、节点和目标</p>
              <p class="max-w-sm text-[0.8125rem] leading-relaxed text-fg-muted">点「添加」打开资源库，点击或拖入卡片；再从卡片右侧的连接点拖线到下一跳。每对连通的入口和目标会生成一项配置。</p>
            </div>
          {/if}
          {#if flash}
            <div
              role={flash.tone === 'danger' ? 'alert' : 'status'}
              data-nopan
              class="absolute top-2 left-1/2 z-40 flex max-w-[calc(100%-1rem)] -translate-x-1/2 cursor-default items-center gap-2 rounded-lg border px-3 py-1.5 text-[0.8125rem] shadow-md {flash.tone === 'danger'
                ? 'border-danger/30 bg-danger-soft text-danger-text'
                : 'border-info/30 bg-info-soft text-info-text'}"
            >
              <span class="min-w-0 break-words">{flash.text}</span>
              <button type="button" class="shrink-0 text-xs underline opacity-80" onclick={() => (flash = null)}>知道了</button>
            </div>
          {/if}
          {#if selection || wire?.needs}
            <Inspector graph={doc.graph} {cat} {resources} {selection} pending={wire?.needs ?? null} {issues} disabled={busy} {apply} onremove={remove} oninsert={insert} onpick={(address) => wire?.needs && finishWire(wire.needs, address)} onclose={() => ((selection = null), (wire = null))} />
          {/if}
        {/snippet}
      </Viewport>
    </div>
  </div>

  <div class="border-t text-[0.8125rem]">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5">
      {#if issues.length}
        <button type="button" class="inline-flex items-center gap-1.5 font-medium {errors.length ? 'text-danger-text' : 'text-warn-text'}" aria-expanded={issuesOpen} onclick={() => (issuesOpen = !issuesOpen)}>
          <TriangleAlert class="size-4" />
          {errors.length ? `${errors.length} 个问题` : ''}{errors.length && warnings.length ? ' · ' : ''}{warnings.length ? `${warnings.length} 个提示` : ''}
          <span class="text-xs font-normal underline">{issuesOpen ? '收起' : '查看'}</span>
        </button>
      {:else}
        <span class="inline-flex items-center gap-1.5 text-ok-text"><CircleCheck class="size-4" />结构检查通过</span>
      {/if}
      <span class="min-w-0 flex-1 text-xs text-fg-muted sm:text-right">拖动卡片只调整本次编辑的视图；保存或「整理」后由系统自动排版。</span>
    </div>
    {#if issuesOpen && issues.length}
      <ul class="max-h-32 overflow-y-auto border-t px-1.5 py-1" aria-label="问题列表">
        {#each issues as issue, index (index)}
          <li>
            <button type="button" class="flex w-full items-baseline gap-2 rounded-md px-1.5 py-1 text-left hover:bg-surface-2" onclick={() => focusIssue(issue)}>
              <span class="shrink-0 rounded-sm px-1 text-2xs font-medium {issue.level === 'error' ? 'bg-danger-soft text-danger-text' : 'bg-warn-soft text-warn-text'}">{issue.level === 'error' ? '问题' : '提示'}</span>
              <span class="min-w-0 flex-1 break-words">{issue.message}</span>
              {#if issue.edge}<span class="shrink-0 text-xs text-fg-muted">{nameOf(issue.edge[0])} → {nameOf(issue.edge[1])}</span>
              {:else if issue.vertex}<span class="shrink-0 text-xs text-fg-muted">{nameOf(issue.vertex)}</span>{/if}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
  {#if narrowNotice}
    <div class="flex items-center gap-2 border-t bg-info-soft px-3 py-1.5 text-xs text-info-text sm:hidden">
      <span class="flex-1">小屏幕上可以点连接点再点目标卡片完成连线；复杂的编辑建议在电脑上进行。</span>
      <button type="button" class="shrink-0 underline" onclick={() => (narrowNotice = false)}>知道了</button>
    </div>
  {/if}
</div>
