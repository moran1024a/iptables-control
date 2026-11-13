<script lang="ts">
  import Activity from '@lucide/svelte/icons/activity';
  import ArrowRight from '@lucide/svelte/icons/arrow-right';
  import Pencil from '@lucide/svelte/icons/pencil';
  import RefreshCw from '@lucide/svelte/icons/refresh-cw';
  import RotateCw from '@lucide/svelte/icons/rotate-cw';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { ApiError, del, errorMessage, get, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { dateTime } from '$lib/format';
  import { overlays } from '$lib/overlay.svelte';
  import { usePoll } from '$lib/poll.svelte';
  import { href, router } from '$lib/router.svelte';
  import { toast } from '$lib/toast.svelte';
  import { entryStyles } from '$lib/topology/colors';
  import { catalogOf, vertexName, vertexOf } from '$lib/topology/model';
  import { configLabel, pathSummary } from '$lib/topology/runtime';
  import type { TopologiesList, Topology, TopologyResources } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import CopyButton from '$ui/CopyButton.svelte';
  import Disclosure from '$ui/Disclosure.svelte';
  import EmptyState from '$ui/EmptyState.svelte';
  import LinkStateBadge from '$ui/LinkStateBadge.svelte';
  import Menu, { type MenuItem } from '$ui/Menu.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Switch from '$ui/Switch.svelte';
  import Tooltip from '$ui/Tooltip.svelte';
  import Page from '../../layout/Page.svelte';
  import Editor from './canvas/Editor.svelte';
  import ViewCanvas from './canvas/ViewCanvas.svelte';

  /** `null` creates a new link (#/routes/new). */
  let { id }: { id: number | null } = $props();
  type Detail = TopologyResources & { link?: Topology };

  let missing = $state(false);
  // svelte-ignore state_referenced_locally
  let editing = $state(id === null);
  let fullscreen = $state(false);
  let busy = $state('');

  // svelte-ignore state_referenced_locally
  const poll = usePoll<Detail>(
    async (signal) => {
      try {
        // A new link only needs the resource lists.
        return id === null ? await get<TopologiesList>('topologies', { signal }) : await get<Detail>(`topologies/${id}`, { signal });
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) missing = true;
        throw err;
      }
    },
    { interval: 5000, paused: () => missing },
  );
  const data = $derived(poll.data);
  const link = $derived(data?.link);
  const deleting = $derived(link?.state === 'deleting');
  const design = $derived(link?.view_graph ?? link?.graph);
  const cat = $derived(catalogOf(data));
  const styles = $derived(design ? entryStyles(design) : new Map());
  const counts = $derived({
    entries: design?.vertices.filter((v) => v.kind === 'entry' && !v.runtime_only).length ?? 0,
    targets: design?.vertices.filter((v) => v.kind === 'target' && !v.runtime_only).length ?? 0,
  });
  const waiting = $derived(link?.configurations.filter((c) => c.state !== 'active' && c.state !== 'error').length ?? 0);
  const failed = $derived(link?.configurations.filter((c) => c.state === 'error').length ?? 0);
  const strays = $derived((link?.warnings ?? []).map((w) => vertexName(vertexOf(link!.graph, w.vertex), cat)));

  // While configurations are being applied, follow the progress closely instead of every 5 s.
  $effect(() => {
    if (!link || editing || link.state === 'active' || link.state === 'error') return;
    const timer = setInterval(() => !document.hidden && !overlays.open && !busy && void poll.refresh(), 1500);
    return () => clearInterval(timer);
  });

  // The page scrolls behind a fullscreen canvas otherwise.
  $effect(() => {
    if (!fullscreen) return;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = '';
    };
  });

  /** Run a link action with a busy marker; a stale revision reloads so the next try works. */
  async function act(name: string, action: (current: Topology) => Promise<void>) {
    if (busy || !link) return;
    busy = name;
    try {
      await action(link);
    } catch (err) {
      toast.error(err instanceof ApiError && err.status === 409 ? `${err.message}（已载入最新状态，请重试）` : errorMessage(err));
    } finally {
      busy = '';
      void poll.refresh();
    }
  }

  const toggleFailover = (enabled: boolean) =>
    act('failover', async (l) => {
      await post(`topologies/${l.id}/action`, { action: 'failover', enabled, revision: l.revision });
      toast.ok(enabled ? '自动灾备已开启' : '自动灾备已关闭');
    });

  const retry = () =>
    act('retry', async (l) => {
      await post(`topologies/${l.id}/action`, { action: 'retry', revision: l.revision });
      toast.ok('已重试同步');
    });

  async function reload() {
    busy = 'reload';
    await poll.refresh();
    busy = '';
    if (poll.error) toast.error(poll.error);
    else toast.ok('已重新加载');
  }

  async function remove() {
    if (!link) return;
    const ok = await confirm({
      title: '删除链路',
      body: `删除「${link.name}」并清理其转发规则？\n删除会先通知各节点清理规则，全部确认后链路才会从列表消失。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    await act('delete', async (l) => {
      const result = await del<{ deleted: boolean }>(`topologies/${l.id}`);
      toast.ok(result.deleted ? '链路已删除' : '正在删除，等待节点清理规则');
      router.go('routes');
    });
  }

  const menu = $derived<MenuItem[]>([
    { label: '重试同步', icon: RotateCw, onselect: retry, disabled: !link?.sync.retry || deleting || !!busy, hint: link?.sync.retry ? undefined : '没有需要重试的配置' },
    { label: '重新加载', icon: RefreshCw, onselect: reload },
    'separator',
    { label: '删除', icon: Trash2, onselect: remove, danger: true, disabled: deleting || !!busy },
  ]);

  async function latest(): Promise<Detail | null> {
    await poll.refresh();
    return poll.error ? null : (poll.data ?? null);
  }

  function saved(savedId: number) {
    fullscreen = false;
    if (id === null) router.go(`routes/${savedId}`);
    else {
      editing = false;
      void poll.refresh();
    }
  }
  function closed() {
    fullscreen = false;
    if (id === null) router.go('routes');
    else {
      editing = false;
      void poll.refresh();
    }
  }
  // A link that starts deleting cannot be edited any more.
  $effect(() => {
    if (id !== null && editing && deleting) editing = false;
  });

  const panel = $derived(
    fullscreen
      ? 'fixed inset-0 z-40 flex flex-col bg-surface sm:inset-3 sm:rounded-xl sm:border sm:shadow-lg'
      : `flex h-[max(30rem,68dvh)] flex-col overflow-hidden rounded-lg border bg-surface shadow-sm ${editing ? 'h-[max(30rem,calc(100dvh-11rem))] lg:h-[max(30rem,calc(100dvh-9.5rem))]' : 'lg:h-[max(30rem,calc(100dvh-15rem))]'}`,
  );
  const failoverText = (c: Topology['configurations'][number]) => ('message' in c.failover ? c.failover : null);
</script>

{#if missing}
  <Page title="链路不存在" back="routes" backLabel="链路">
    <EmptyState title="链路不存在" text="它可能已被删除，或节点已完成规则清理。">
      <Button href={href('routes')}>返回链路</Button>
    </EmptyState>
  </Page>
{:else if !data || (id !== null && !link)}
  <Page title={id === null ? '新建链路' : '链路'} back="routes" backLabel="链路" wide>
    {#if poll.error}
      <Alert tone="danger" title="加载失败">
        {poll.error}
        {#snippet actions()}<Button size="sm" onclick={() => poll.refresh()}>重试</Button>{/snippet}
      </Alert>
    {:else}
      <div class="flex justify-center py-24 text-fg-subtle"><Spinner class="size-5" /></div>
    {/if}
  </Page>
{:else}
  <Page title={editing ? (link ? '编辑链路' : '新建链路') : (link?.name ?? '')} subtitle={editing && link ? link.name : undefined} back="routes" backLabel="链路" wide>
    {#snippet meta()}
      {#if link && !editing}<LinkStateBadge state={link.state} />{/if}
    {/snippet}
    {#snippet actions()}
      {#if link && !editing}
        <Tooltip text="节点失联超过设定时间后，自动把经过它的配置切到备用路径">
          <label class="flex h-8 items-center gap-2 rounded-md border border-border-strong/70 bg-surface px-2.5 text-sm font-medium shadow-sm {deleting ? 'opacity-60' : ''}">
            自动灾备
            {#if busy === 'failover'}<Spinner class="size-3.5" />{/if}
            <Switch checked={!!link.failover_enabled} disabled={deleting || !!busy} label="自动灾备" onchange={toggleFailover} />
          </label>
        </Tooltip>
        <Button href={href(`routes/${link.id}/quality`)}><Activity />网络质量</Button>
        <Button onclick={() => (editing = true)} disabled={deleting || !!busy} title={deleting ? '链路正在删除' : undefined}><Pencil />编辑</Button>
        <Menu items={menu} />
      {/if}
    {/snippet}

    {#if poll.error}<Alert tone="warn">刷新失败，显示的可能不是最新状态：{poll.error}</Alert>{/if}

    <section class={panel} aria-label="拓扑">
      {#if editing}
        <Editor {link} resources={data} bind:fullscreen onclose={closed} onsaved={saved} {latest} />
      {:else if link}
        <ViewCanvas {link} resources={data} busy={!!busy} bind:fullscreen onchanged={() => poll.refresh()} />
      {/if}
    </section>

    {#if link && !editing}
      {#if link.state !== 'active' || link.warnings.length}
        <Alert tone={link.state === 'error' ? 'danger' : link.state === 'active' ? 'info' : 'warn'}>
          <ul class="flex flex-col gap-0.5">
            {#if deleting}<li>正在删除：等待节点确认规则清理，完成后链路会自动消失。</li>{/if}
            {#if waiting && !deleting}<li>{waiting} 项配置等待生效</li>{/if}
            {#if failed}<li>{failed} 项配置异常{link.error ? `：${link.error}` : ''}</li>{:else if link.error}<li>{link.error}</li>{/if}
            {#if strays.length}<li>{strays.length} 个节点没有可达的入口—目标路径：{strays.join('、')}</li>{/if}
          </ul>
          {#snippet actions()}
            {#if link.sync.retry && !deleting}<Button size="sm" loading={busy === 'retry'} disabled={!!busy} onclick={retry}>重试同步</Button>{/if}
          {/snippet}
        </Alert>
      {/if}

      <section class="rounded-lg border bg-surface shadow-sm" aria-label="路径状态">
        <header class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2.5">
          <h2 class="text-[0.8667rem] font-semibold">路径状态</h2>
          <span class="text-xs text-fg-muted">{counts.entries} 个入口 · {counts.targets} 个目标</span>
          <span class="flex-1"></span>
          <a href={href('configs')} class="inline-flex items-center gap-1 text-xs font-medium text-accent-text hover:underline">
            查看配置 · {link.configurations.length}<ArrowRight class="size-3.5" />
          </a>
        </header>
        {#if !link.configurations.length}
          <p class="px-4 py-6 text-center text-[0.8667rem] text-fg-muted">还没有配置。入口连通到目标并保存后，每对入口和目标生成一项配置。</p>
        {:else}
          <ul class="divide-y">
            {#each link.configurations as c (c.id)}
              {@const label = configLabel(c, design!, cat)}
              {@const path = pathSummary(c.active_path, design!, cat)}
              {@const next = c.pending_path.length > 1 && c.pending_path.join('/') !== c.active_path.join('/') ? pathSummary(c.pending_path, design!, cat) : null}
              {@const style = styles.get(c.entry)}
              {@const failover = failoverText(c)}
              <li class="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-x-2.5 gap-y-0.5 px-4 py-2.5 text-[0.8667rem] lg:grid-cols-[auto_minmax(0,1.1fr)_minmax(0,1.4fr)_auto_auto] lg:items-center">
                <span class="tabular mt-0.5 flex size-5 items-center justify-center rounded-full text-2xs font-bold text-white lg:mt-0" style:background={style?.color ?? 'var(--idle)'}>
                  {style?.ordinal ?? ''}
                </span>
                <div class="min-w-0">
                  <p class="truncate font-medium">
                    {label.entry} <span class="text-fg-subtle">→</span>
                    {label.target}{#if label.address}<span class="font-normal text-fg-muted"> · {label.address}</span>{/if}
                  </p>
                  <p class="truncate text-xs text-fg-muted">{c.code} · {c.name}</p>
                </div>
                <div class="col-start-2 min-w-0 lg:col-start-3">
                  <p class="truncate {c.active_path.length ? '' : 'text-fg-muted'}" title={path.full}>
                    {#if c.active_path.length > 1}<span class="mr-1 text-xs text-fg-muted">经</span>{/if}{path.text}
                  </p>
                  {#if next}<p class="truncate text-xs text-warn-text" title={next.full}>正在切换到：{next.text}</p>{/if}
                  {#if c.error}
                    <p class="text-xs break-words text-danger-text">{c.error}</p>
                  {:else if failover}
                    <p class="text-xs break-words {failover.phase === 'done' ? 'text-fg-muted' : 'text-warn-text'}">
                      自动灾备：{failover.message}{failover.phase === 'paused' ? '（重试同步、切换线路或重新开关自动灾备可恢复）' : ''}
                    </p>
                  {/if}
                </div>
                <div class="col-start-2 flex min-w-0 items-center gap-1 lg:col-start-4">
                  {#if c.connection}
                    <code class="truncate text-xs text-fg-muted" title="入口地址">{c.connection.host}:{c.connection.port}</code>
                    <CopyButton text={c.connection.uri} size="xs" />
                  {:else if c.port}
                    <code class="text-xs text-fg-muted" title="入口端口">:{c.port}</code>
                  {/if}
                </div>
                <div class="col-start-3 row-start-1 justify-self-end lg:col-start-5"><LinkStateBadge state={c.state} /></div>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <Disclosure title="切换记录" hint={link.events.length ? `最近 ${link.events.length} 条` : undefined} key="link-events-{link.id}">
        {#if link.events.length}
          <ol class="flex flex-col gap-1 text-[0.8667rem]">
            {#each link.events as event, index (index)}
              <li class="flex gap-3"><time class="tabular shrink-0 text-fg-muted">{dateTime(event.ts)}</time><span class="min-w-0 break-words">{event.message}</span></li>
            {/each}
          </ol>
        {:else}
          <p class="text-[0.8667rem] text-fg-muted">暂无记录</p>
        {/if}
      </Disclosure>
    {/if}
  </Page>
{/if}
