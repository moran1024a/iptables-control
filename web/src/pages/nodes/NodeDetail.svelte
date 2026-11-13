<script lang="ts">
  import KeyRound from '@lucide/svelte/icons/key-round';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Stethoscope from '@lucide/svelte/icons/stethoscope';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { ApiError, del, errorMessage, get, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { ago, dateTime, signed } from '$lib/format';
  import { usePoll } from '$lib/poll.svelte';
  import { href, router } from '$lib/router.svelte';
  import { MODE_LABEL } from '$lib/status';
  import { toast } from '$lib/toast.svelte';
  import type { NodeDeleted, NodeDetail, NodesList } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import Disclosure from '$ui/Disclosure.svelte';
  import Menu, { type MenuItem } from '$ui/Menu.svelte';
  import NodeStatusBadge from '$ui/NodeStatusBadge.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import TagList from '$ui/TagList.svelte';
  import Page from '../../layout/Page.svelte';
  import DiagnosticsDialog from './DiagnosticsDialog.svelte';
  import InstallPanel from './InstallPanel.svelte';
  import KeyDialog from './KeyDialog.svelte';
  import { AGENT_UPDATE_TEXT, installed } from './node-logic';
  import NodeForm from './NodeForm.svelte';
  import NodeStatusCard from './NodeStatusCard.svelte';
  import TelemetryPanel from './TelemetryPanel.svelte';

  let { id }: { id: number } = $props();

  let gone = false;
  const poll = usePoll(
    async (signal) => {
      try {
        return await get<NodeDetail>(`nodes/${id}`, { signal });
      } catch (err) {
        if (err instanceof ApiError && err.status === 404 && !gone) {
          // Deleted here, elsewhere, or confirmed by the agent while this page was open.
          gone = true;
          toast.info('节点不存在或已删除');
          router.go('nodes', { replace: true });
        }
        throw err;
      }
    },
    { interval: 5000 },
  );
  const detail = $derived(poll.data);
  const node = $derived(detail?.node);

  // Server time, advanced locally between polls so relative times do not jump in 5 s steps.
  let tick = $state(Date.now());
  $effect(() => {
    const timer = setInterval(() => (tick = Date.now()), 1000);
    return () => clearInterval(timer);
  });
  const now = $derived(detail ? detail.now + Math.max(0, Math.floor((tick - poll.loadedAt) / 1000)) : 0);

  // Local UI state: showing the key never talks to the server.
  let revealed = $state(false);
  let busy = $state<'' | 'edit' | 'rekey' | 'delete'>('');

  const ready = $derived(node ? installed(node) : false);
  const extra = $derived(node?.addresses.filter((address) => !address.primary) ?? []);
  const showTelemetry = $derived(!!detail && (detail.node.installed_agent === 'active' || (detail.telemetry.traffic?.length ?? 0) > 0));
  const canDiagnose = $derived(node?.installed_agent === 'active' && node.desired_mode === 'active');
  /** One entry per configuration, however many rules it has on this node. */
  const configs = $derived([...new Map((detail?.configs ?? []).map((config) => [config.id, config])).values()]);
  const updateText = $derived(node ? (AGENT_UPDATE_TEXT[node.agent_update] ?? '') : '');
  const label = $derived(node ? `「${node.name}」` : '');

  // ---- edit ----
  let formOpen = $state(false);
  let catalogue = $state.raw<Pick<NodesList, 'groups' | 'tags'>>({ groups: [], tags: [] });
  async function edit() {
    if (busy) return;
    busy = 'edit';
    try {
      // Groups and the tag catalogue only come with the list; refresh the node too so the form starts from current values.
      const [list] = await Promise.all([get<NodesList>('nodes'), poll.refresh()]);
      catalogue = { groups: list.groups, tags: list.tags };
      formOpen = true;
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      busy = '';
    }
  }

  // ---- rekey ----
  let keyOpen = $state(false);
  let newKey = $state('');
  async function rekey() {
    if (busy || !node) return;
    const ok = await confirm({
      title: '重置密钥',
      body: `重置后旧密钥立即失效，节点 ${label} 需要填入新密钥才能继续同步。`,
      confirmLabel: '重置',
      danger: true,
    });
    if (!ok) return;
    busy = 'rekey';
    try {
      newKey = (await post<{ key: string }>(`nodes/${id}/rekey`)).key;
      keyOpen = true;
      void poll.refresh();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      busy = '';
    }
  }

  // ---- delete ----
  function deleted(result: NodeDeleted) {
    if (result.deleted) {
      gone = true;
      toast.ok('已删除');
      router.go('nodes');
    } else {
      toast.info('已下发空配置，节点确认后自动删除');
      void poll.refresh();
    }
  }
  async function remove() {
    if (busy || !node) return;
    // The server removes a node at once only when it has never connected.
    const ok = await confirm({
      title: '删除节点',
      body: node.first_seen ? `将向节点 ${label} 下发空配置，节点确认清空规则后自动删除。` : `确定删除节点 ${label}？此节点从未连接，会立即删除。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    busy = 'delete';
    try {
      try {
        deleted(await del<NodeDeleted>(`nodes/${id}`));
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 409 && err.field === 'force')) throw err;
        const force = await confirm({
          title: '强制删除节点',
          body: `${err.message}。如果节点 ${label} 已永久失联，可以强制删除：记录会直接删除，节点上的规则不会被清理。`,
          confirmLabel: '强制删除',
          danger: true,
        });
        if (force) deleted(await del<NodeDeleted>(`nodes/${id}?force=1`));
      }
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      busy = '';
    }
  }
  async function forceRemove() {
    if (busy || !node) return;
    const ok = await confirm({
      title: '强制删除节点',
      body: `节点 ${label} 尚未确认清空规则。强制删除会直接删除记录，节点上的规则不会被清理。`,
      confirmLabel: '强制删除',
      danger: true,
    });
    if (!ok) return;
    busy = 'delete';
    try {
      deleted(await del<NodeDeleted>(`nodes/${id}?force=1`));
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      busy = '';
    }
  }

  let diagnosticsOpen = $state(false);
  const menu = $derived.by((): MenuItem[] => {
    const items: MenuItem[] = [];
    if (canDiagnose) items.push({ label: '节点诊断', icon: Stethoscope, onselect: () => (diagnosticsOpen = true) });
    items.push({ label: '重置密钥', icon: KeyRound, onselect: rekey, disabled: !!busy });
    items.push('separator');
    items.push(
      node?.status === 'deleting'
        ? { label: '强制删除', icon: Trash2, onselect: forceRemove, danger: true, disabled: !!busy }
        : { label: '删除', icon: Trash2, onselect: remove, danger: true, disabled: !!busy },
    );
    return items;
  });
</script>

{#if !detail || !node}
  <Page title="节点" back="nodes" backLabel="节点列表">
    {#if poll.error}
      <Alert tone="danger">
        {poll.error}
        {#snippet actions()}<Button size="sm" onclick={poll.refresh}>重试</Button>{/snippet}
      </Alert>
    {:else}
      <div class="flex justify-center py-16 text-fg-subtle"><Spinner class="size-5" /></div>
    {/if}
  </Page>
{:else}
  {#snippet addressCard()}
      <Card title="IP 地址" hint={extra.length ? `${node.addresses.length} 个` : undefined} flush>
        <ul class="divide-y">
          {#each node.addresses as address (address.id)}
            <li class="flex flex-wrap items-center gap-x-2.5 gap-y-1 px-4 py-2.5 text-[0.8667rem]">
              <span class="w-9 shrink-0 text-xs {address.primary ? 'font-medium text-accent-text' : 'text-fg-muted'}">{address.primary ? '主 IP' : '副 IP'}</span>
              <code class="font-mono break-all">{address.host}</code>
              {#if address.name}<span class="text-fg-muted">{address.name}</span>{/if}
              <span class="flex flex-wrap gap-x-2.5 text-xs text-fg-subtle sm:ml-auto">
                {#if address.local && address.local !== address.host}<span>源 IP <code class="font-mono">{address.local}</code></span>{/if}
                {#if address.device}<span>网卡 <code class="font-mono">{address.device}</code></span>{/if}
                {#if address.gateway}<span>网关 <code class="font-mono">{address.gateway}</code></span>{/if}
              </span>
            </li>
          {:else}
            <li class="px-4 py-2.5 text-[0.8667rem]"><code class="font-mono">{node.addr}</code></li>
          {/each}
        </ul>
        <p class="border-t px-4 py-2 text-xs text-fg-muted">
          本机地址最近识别 {dateTime(node.network.sampled_at)}{#if node.src_ip} · 连接来源 <code class="font-mono">{node.src_ip}</code>{/if}
        </p>
      </Card>
  {/snippet}
  {#snippet rulesCard()}
      <Card title="转发规则" hint="允许端口 {node.ports}" flush>
        {#if detail.rules.length}
          <div class="overflow-x-auto">
            <table class="data-table">
              <thead><tr><th class="num">入站端口</th><th>转发到</th><th>所属配置</th></tr></thead>
              <tbody>
                {#each detail.rules as rule (rule.id)}
                  <tr>
                    <td class="num font-mono">{rule.port}</td>
                    <td class="font-mono break-all">{rule.host}:{rule.tport}</td>
                    <td>
                      <a href={href('configs')} class="text-accent-text hover:underline">{rule.configuration_name}</a>
                      {#if rule.phase === 'stage'}<span class="ml-1 text-xs text-fg-subtle" title="新路径的规则已下发，尚未切换为生效">准备中</span>{/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <p class="px-4 py-6 text-center text-[0.8667rem] text-fg-muted">{node.status === 'deleting' ? '正在删除，已下发空配置' : '没有转发规则'}</p>
        {/if}
      </Card>
  {/snippet}

  <Page
    title={node.name}
    subtitle="{node.addr}{extra.length ? ` ＋${extra.length} IP` : ''} · {node.group}{node.weight ? ` · 权重 ${signed(node.weight)}` : ''}"
    back="nodes"
    backLabel="节点列表"
  >
    {#snippet meta()}
      <TagList tags={node.tags} limit={3} />
      <span
        class="shrink-0 rounded-sm border px-1 text-2xs leading-4 font-medium {node.desired_mode === 'active' ? 'border-accent/30 text-accent-text' : 'text-fg-muted'}"
        title={node.desired_mode === 'active' ? '主动：长连接自动同步' : '被动：在节点上手动同步，可选远程唤醒'}
      >
        {MODE_LABEL[node.desired_mode]}
      </span>
      <NodeStatusBadge status={node.status} />
      <span class="tabular text-xs text-fg-subtle">
        {#if node.last_seen}{node.desired_mode === 'passive' ? '最近同步' : '最后上报'} {ago(node.last_seen, now)}{:else}尚未连接{/if}
      </span>
    {/snippet}
    {#snippet actions()}
      {#if busy === 'rekey' || busy === 'delete'}
        <span class="inline-flex items-center gap-1.5 text-xs text-fg-muted" role="status"><Spinner />处理中…</span>
      {/if}
      <Button onclick={edit} loading={busy === 'edit'} disabled={!!busy}>
        {#if busy !== 'edit'}<Pencil />{/if}编辑
      </Button>
      <Menu items={menu} />
    {/snippet}

    {#if poll.error}
      <Alert tone="warn">刷新失败：{poll.error}。显示的是上一次的数据。</Alert>
    {/if}
    {#if node.note}
      <p class="rounded-md border-l-2 border-border-strong bg-surface-2/60 px-3 py-2 text-[0.8667rem] leading-relaxed break-words whitespace-pre-wrap text-fg-muted">
        <span class="sr-only">备注：</span>{node.note}
      </p>
    {/if}

    {#if !ready && node.status !== 'deleting'}
      <InstallPanel {detail} hero bind:revealed />
    {/if}

    <div class="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div class="flex min-w-0 flex-col gap-4">
        <NodeStatusCard {detail} {now} busy={!!busy} onforce={forceRemove} />
        {#if ready}{@render rulesCard()}{:else}{@render addressCard()}{/if}
      </div>
      <div class="flex min-w-0 flex-col gap-4">
        {#if ready || node.status === 'deleting'}
          <InstallPanel {detail} bind:revealed />
        {/if}
        {#if ready}{@render addressCard()}{:else}{@render rulesCard()}{/if}
      </div>
    </div>

    {#if showTelemetry}
      <TelemetryPanel {detail} ondiagnose={canDiagnose ? () => (diagnosticsOpen = true) : undefined} />
    {/if}

    <div class="grid items-start gap-4 lg:grid-cols-2">
      <Disclosure title="更多信息" key="more-{id}">
        <dl class="kv">
          <dt>分组</dt><dd>{node.group}</dd>
          <dt>权重</dt><dd class="tabular">{signed(node.weight)}</dd>
          <dt>同步模式</dt><dd>{MODE_LABEL[node.desired_mode]}{node.manual_required ? '（切换待确认）' : ''}</dd>
          <dt>质量探测端口</dt><dd class="tabular">{node.quality_port ? `UDP ${node.quality_port}` : '已关闭'}</dd>
          <dt>来源 IP</dt><dd class="font-mono">{node.src_ip || '-'}</dd>
          <dt>时差</dt><dd class="tabular">{node.skew === null ? '-' : `${signed(node.skew)} 秒`}</dd>
          <dt>节点端版本</dt>
          <dd>
            {node.agent || '-'}
            {#if updateText}<span class="text-fg-muted">（目标转发 Agent 为 {detail.forward_version}，{updateText}）</span>{/if}
          </dd>
          <dt>已安装组件</dt><dd>{node.installed_agent || 'unknown'} / {node.backend || 'iptables'}</dd>
          <dt>管理连接</dt><dd>{node.connection_state || '旧版轮询'}</dd>
          <dt>确认进度</dt><dd class="tabular">{node.applied_generation} / {node.desired_generation}</dd>
          <dt>已应用版本</dt><dd class="font-mono">{node.reported_v || '-'}</dd>
          <dt>应有版本</dt><dd class="font-mono">{node.want_v}</dd>
          <dt>上报状态</dt><dd>{node.state || '-'}</dd>
          <dt>错误信息</dt><dd>{node.err || '-'}</dd>
          <dt>首次连接</dt><dd class="tabular">{dateTime(node.first_seen)}</dd>
          <dt>使用该节点的配置</dt><dd>{configs.map((config) => config.name).join('、') || '-'}</dd>
        </dl>
      </Disclosure>
      <Disclosure title="事件记录" hint={detail.events.length ? `${detail.events.length} 条` : undefined} key="events-{id}">
        {#if detail.events.length}
          <ol class="-my-1 max-h-96 divide-y divide-dashed overflow-y-auto text-[0.8667rem]">
            {#each detail.events as event, index (index)}
              <li class="flex flex-wrap gap-x-3 py-1.5">
                <time class="tabular shrink-0 text-xs leading-5 text-fg-subtle">{dateTime(event.ts)}</time>
                <span class="min-w-0 break-words">{event.text}</span>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="text-[0.8667rem] text-fg-muted">暂无事件</p>
        {/if}
      </Disclosure>
    </div>
  </Page>

  <NodeForm bind:open={formOpen} {node} groups={catalogue.groups} tags={catalogue.tags} onsaved={() => void poll.refresh()} />
  <KeyDialog bind:open={keyOpen} key={newKey} name={node.name} />
  {#if canDiagnose}
    <DiagnosticsDialog bind:open={diagnosticsOpen} nodeId={id} nodeName={node.name} rules={detail.rules} />
  {/if}
{/if}
