<script lang="ts" module>
  import type { Edge } from '$lib/topology/model';
  import type { VertexId } from '$lib/types';
  export type Selection = { vertex: VertexId; edge?: undefined } | { edge: Edge; vertex?: undefined } | null;
</script>

<script lang="ts">
  import Plus from '@lucide/svelte/icons/plus';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import X from '@lucide/svelte/icons/x';
  import { signed } from '$lib/format';
  import { NODE_STATUS } from '$lib/status';
  import { replaceResource, setEdgeAddress, setEntryAddress, setPort, setWeight } from '$lib/topology/edit';
  import { addressesOf, addressText, boundAddress, connections, nodeOf, vertexName, vertexOf, type AddressRow, type Catalog } from '$lib/topology/model';
  import type { Issue } from '$lib/topology/rules';
  import { AUTO, type Graph, type NodeAddress, type PaletteNode, type TopologyResources } from '$lib/types';
  import Badge from '$ui/Badge.svelte';
  import Button from '$ui/Button.svelte';
  import Field from '$ui/Field.svelte';
  import Input from '$ui/Input.svelte';
  import Select from '$ui/Select.svelte';

  type Props = {
    graph: Graph;
    cat: Catalog;
    resources: TopologyResources;
    selection: Selection;
    /** A wire waiting for the receive IP of this vertex. */
    pending: VertexId | null;
    issues: Issue[];
    disabled?: boolean;
    /** Apply a new graph; edits with the same `coalesce` key form one undo step. */
    apply: (next: Graph, coalesce?: string) => void;
    onremove: () => void;
    oninsert: () => void;
    onpick: (address: string) => void;
    onclose: () => void;
  };
  let { graph, cat, resources, selection, pending, issues, disabled = false, apply, onremove, oninsert, onpick, onclose }: Props = $props();

  const vertex = $derived(selection?.vertex ? vertexOf(graph, selection.vertex) : undefined);
  const edge = $derived(selection?.edge ?? null);
  const from = $derived(edge ? vertexOf(graph, edge[0]) : undefined);
  const to = $derived(edge ? vertexOf(graph, edge[1]) : undefined);
  const node = $derived(nodeOf(vertex, cat));
  const mine = $derived(
    issues.filter((i) => (vertex && i.vertex === vertex.id && (!i.edge || i.level === 'error')) || (edge && i.edge && i.edge[0] === edge[0] && i.edge[1] === edge[1])),
  );

  const grouped = (items: { id: number; name: string; group_id: number }[], groups: { id: number; name: string }[]) =>
    items.map((item) => ({
      value: item.id,
      label: groups.length > 1 ? `${groups.find((g) => g.id === item.group_id)?.name ?? ''} / ${item.name}` : item.name,
    }));
  const resourceOptions = $derived.by(() => {
    if (!vertex || vertex.kind === 'direct') return [];
    const options = vertex.kind === 'target' ? grouped(resources.targets, resources.target_groups) : grouped(resources.nodes, resources.node_groups);
    const current = vertex.kind === 'target' ? vertex.target_id : vertex.node_id;
    return options.some((o) => o.value === current) ? options : [{ value: current ?? -1, label: '已删除' }, ...options];
  });

  /** Why an address cannot be forced as the source of outgoing traffic yet. */
  function egressProblem(a: NodeAddress, network: PaletteNode['network'] | undefined): boolean {
    const local = a.local || a.host;
    if (network?.complete && !network.addresses.some((n) => n.local === local)) return true;
    if (!a.local || !a.device) return true;
    return !!network?.complete && !network.addresses.some((n) => n.local === local && n.device === a.device);
  }
  const addressLabel = (a: AddressRow) => addressText(a) + (a.name ? ` · ${a.name}` : a.primary ? ' · 主 IP' : '');
  const receiveOptions = (rows: AddressRow[], chosen: string | null) => [
    ...(chosen && rows.some((a) => a.id === chosen) ? [] : [{ value: '', label: '选择地址' }]),
    ...rows.map((a) => ({ value: a.id, label: addressLabel(a) })),
  ];
  const egressOptions = $derived.by(() => {
    if (!from || from.kind === 'direct') return [];
    const network = nodeOf(from, cat)?.network;
    return [
      { value: AUTO, label: '自动出口' },
      ...addressesOf(graph, from, cat).map((a) => ({ value: a.id, label: addressLabel(a) + (egressProblem(a as NodeAddress, network) ? ' · 出口待设置' : '') })),
    ];
  });

  // Targets an entry can reach: one listen port per pair.
  const portTargets = $derived.by(() => {
    if (vertex?.kind !== 'entry') return [];
    const seen = new Set([vertex.id]);
    const stack = [vertex.id];
    const edges = connections(graph);
    while (stack.length) {
      const at = stack.pop()!;
      for (const [a, b] of edges) if (a === at && !seen.has(b)) (seen.add(b), stack.push(b));
    }
    return graph.vertices.filter((v) => v.kind === 'target' && seen.has(v.id));
  });

  const number = (text: string) => (text.trim() === '' ? NaN : Number(text));
  const pendingVertex = $derived(vertexOf(graph, pending));
</script>

<aside
  data-nopan
  data-scroll
  aria-label="属性"
  class="absolute inset-x-2 bottom-2 z-40 flex max-h-[60%] cursor-default flex-col rounded-lg border bg-surface text-[0.8667rem] shadow-lg select-text sm:inset-x-auto sm:top-2 sm:right-2 sm:bottom-auto sm:max-h-[calc(100%-1rem)] sm:w-64"
>
  <header class="flex items-center gap-2 border-b py-1.5 pr-1.5 pl-3">
    <p class="min-w-0 flex-1 truncate font-semibold">
      {#if pendingVertex}连接到 {vertexName(pendingVertex, cat)}
      {:else if vertex}{vertex.kind === 'direct' ? 'direct' : vertexName(vertex, cat)}
      {:else if edge}{vertexName(from, cat)} → {vertexName(to, cat)}{/if}
    </p>
    <button type="button" class="flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3" aria-label="关闭" onclick={onclose}><X class="size-4" /></button>
  </header>

  <div class="flex min-h-0 flex-col gap-3 overflow-y-auto p-3">
    {#if pendingVertex}
      <Field label="接收 IP" hint="也可以直接点卡片上的 IP 行">
        {#snippet children({ id })}
          <Select {id} value="" options={receiveOptions(addressesOf(graph, pendingVertex, cat), null)} class="w-full" aria-label="选择接收 IP" onchange={(e) => e.currentTarget.value && onpick(e.currentTarget.value)} />
        {/snippet}
      </Field>
    {:else if vertex?.kind === 'direct'}
      <Field label="权重" hint="-100000 至 100000 的整数；路径总权重高者优先" error={mine.find((i) => i.message.startsWith('权重'))?.message}>
        {#snippet children({ id, invalid })}
          <Input
            {id}
            type="number"
            min="-100000"
            max="100000"
            step="1"
            {invalid}
            {disabled}
            aria-label="direct 权重"
            value={Number.isFinite(vertex.weight) ? String(vertex.weight) : ''}
            oninput={(e) => apply(setWeight(graph, vertex.id, number(e.currentTarget.value)), `weight:${vertex.id}`)}
          />
        {/snippet}
      </Field>
      <Button variant="danger-ghost" size="sm" {disabled} onclick={onremove}><Trash2 />移除 direct</Button>
    {:else if vertex}
      <Field label={vertex.kind === 'target' ? '更换目标' : '更换节点'}>
        {#snippet children({ id })}
          <Select
            {id}
            {disabled}
            value={(vertex.kind === 'target' ? vertex.target_id : vertex.node_id) ?? -1}
            options={resourceOptions}
            class="w-full"
            onchange={(e) => apply(replaceResource(graph, cat, vertex.id, Number(e.currentTarget.value)))}
          />
        {/snippet}
      </Field>
      {#if node}
        <p class="flex flex-wrap items-center gap-1.5 text-xs text-fg-muted">
          <Badge tone={NODE_STATUS[node.status].tone} dot>{NODE_STATUS[node.status].label}</Badge>
          全局权重 {signed(node.weight)}（在节点页修改）
        </p>
      {/if}
      {#if vertex.kind === 'entry'}
        {@const rows = addressesOf(graph, vertex, cat)}
        <Field label="入口接收 IP" error={mine.find((i) => i.message.includes('接收 IP'))?.message}>
          {#snippet children({ id, invalid })}
            <Select
              {id}
              {invalid}
              {disabled}
              value={vertex.address && rows.some((a) => a.id === vertex.address) ? vertex.address : ''}
              options={receiveOptions(rows, vertex.address ?? null)}
              class="w-full"
              onchange={(e) => apply(setEntryAddress(graph, vertex.id, e.currentTarget.value || null))}
            />
          {/snippet}
        </Field>
        {#each portTargets as target (target.id)}
          {@const port = graph.ports?.[`${vertex.id}/${target.id}`]}
          <Field label="入口端口 → {vertexName(target, cat)}" hint="留空自动分配">
            {#snippet children({ id })}
              <Input
                {id}
                type="number"
                min="1"
                max="65535"
                step="1"
                placeholder="自动端口"
                {disabled}
                invalid={port !== undefined && !(Number.isInteger(port) && port >= 1 && port <= 65535)}
                title="入口端口，留空自动分配"
                value={port !== undefined && Number.isFinite(port) ? String(port) : ''}
                oninput={(e) => apply(setPort(graph, vertex.id, target.id, e.currentTarget.value.trim() === '' ? null : Number(e.currentTarget.value)), `port:${vertex.id}/${target.id}`)}
              />
            {/snippet}
          </Field>
        {/each}
      {/if}
      <Button variant="danger-ghost" size="sm" {disabled} onclick={onremove}><Trash2 />删除</Button>
    {:else if edge && from && to}
      {#if from.kind !== 'direct'}
        <Field label="发出 IP" hint="自动出口由系统路由决定源地址">
          {#snippet children({ id })}
            <Select {id} {disabled} value={boundAddress(graph, edge, 'out') ?? AUTO} options={egressOptions} class="w-full" onchange={(e) => apply(setEdgeAddress(graph, edge, 'out', e.currentTarget.value))} />
          {/snippet}
        </Field>
      {/if}
      {#if to.kind !== 'direct'}
        {@const rows = addressesOf(graph, to, cat)}
        {@const chosen = boundAddress(graph, edge, 'in')}
        <Field label="接收地址" error={mine.find((i) => i.message.includes('接收 IP'))?.message}>
          {#snippet children({ id, invalid })}
            <Select
              {id}
              {invalid}
              {disabled}
              value={chosen && rows.some((a) => a.id === chosen) ? chosen : ''}
              options={receiveOptions(rows, chosen)}
              class="w-full"
              onchange={(e) => apply(setEdgeAddress(graph, edge, 'in', e.currentTarget.value || null))}
            />
          {/snippet}
        </Field>
      {/if}
      <div class="flex flex-wrap gap-2">
        {#if from.kind !== 'direct' && to.kind !== 'direct'}
          <Button size="sm" {disabled} onclick={oninsert} title="在这条连线上插入 direct，用来调整权重"><Plus />direct</Button>
        {/if}
        <Button variant="danger-ghost" size="sm" {disabled} onclick={onremove}><Trash2 />删除连线</Button>
      </div>
    {/if}

    {#each mine.filter((i) => !i.message.includes('接收 IP') && !i.message.startsWith('权重')) as issue (issue.message)}
      <p class="text-xs {issue.level === 'error' ? 'text-danger-text' : 'text-warn-text'}" role={issue.level === 'error' ? 'alert' : undefined}>{issue.message}</p>
    {/each}
  </div>
</aside>
