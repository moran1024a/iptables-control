<script lang="ts">
  import ArrowRight from '@lucide/svelte/icons/arrow-right';
  import Check from '@lucide/svelte/icons/check';
  import Copy from '@lucide/svelte/icons/copy';
  import Hourglass from '@lucide/svelte/icons/hourglass';
  import MapPin from '@lucide/svelte/icons/map-pin';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import Waypoints from '@lucide/svelte/icons/waypoints';
  import { copyText } from '$lib/clipboard';
  import { href, router } from '$lib/router.svelte';
  import type { SubscriptionCandidate } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import LinkStateBadge from '$ui/LinkStateBadge.svelte';
  import Menu, { type MenuItem } from '$ui/Menu.svelte';
  import TagList from '$ui/TagList.svelte';
  import { compatibility, hostPort, protocolName, relayCount, targetExtra, type AnyConfig } from './model';

  type Props = {
    config: AnyConfig;
    /** Subscription export status of this configuration, when known. */
    candidate?: SubscriptionCandidate;
    /** Names of the subscriptions that include it. */
    usedBy?: string[];
    onedit: (config: AnyConfig) => void;
    ondelete?: (config: AnyConfig) => void;
  };
  let { config, candidate, usedBy = [], onedit, ondelete }: Props = $props();

  const connection = $derived(config.connection);
  const address = $derived(connection ? hostPort(connection.host, connection.port) : '');
  /** Plain endpoints are used as an address; everything else is imported into a client as a share link. */
  const plain = $derived(connection?.protocol === 'endpoint');
  const relays = $derived(config.source === 'topology' ? relayCount(config) : 0);
  const extra = $derived(config.source === 'topology' ? targetExtra(config) : '');
  // "Not applied yet" is already what the row says when there is no ingress address.
  const compat = $derived(connection ? compatibility(candidate) : { tone: 'none' as const, text: '' });

  let copied = $state(false);
  let timer: ReturnType<typeof setTimeout>;
  async function copy() {
    if (!connection) return;
    if (await copyText(connection.uri, plain ? '已复制入口地址' : '已复制分享链接')) {
      copied = true;
      clearTimeout(timer);
      timer = setTimeout(() => (copied = false), 1500);
    }
  }

  const menu = $derived.by(() => {
    const items: MenuItem[] = [];
    if (connection && !plain) items.push({ label: '只复制地址和端口', icon: MapPin, onselect: () => void copyText(address, '已复制地址') });
    if (config.source === 'topology') {
      const id = config.topology_id;
      items.push({ label: '打开所属链路', icon: Waypoints, onselect: () => router.go(`routes/${id}`) });
    } else if (ondelete) {
      if (items.length) items.push('separator');
      items.push({ label: '删除', icon: Trash2, danger: true, onselect: () => ondelete(config) });
    }
    return items;
  });
</script>

<div class="grid grid-cols-1 items-center gap-x-4 gap-y-2 px-4 py-2.5 md:grid-cols-[minmax(0,1fr)_minmax(0,25rem)]">
  <div class="min-w-0">
    <div class="flex min-w-0 items-center gap-2">
      <code class="shrink-0 rounded-sm bg-surface-3 px-1 text-2xs leading-4 font-medium text-fg-muted">{config.code}</code>
      <span class="truncate text-sm font-medium" title={config.name}>{config.name}</span>
      {#if config.source === 'topology'}<LinkStateBadge state={config.state} />{/if}
    </div>
    <div class="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-fg-muted">
      {#if config.source === 'topology'}
        <a href={href(`routes/${config.topology_id}`)} class="max-w-40 truncate font-medium text-fg-muted underline-offset-2 hover:text-accent-text hover:underline" title="所属链路：{config.topology_name}">
          {config.topology_name}
        </a>
        <span class="text-fg-subtle">·</span>
        <span class="inline-flex min-w-0 flex-wrap items-center gap-1">
          <span class="truncate">{config.entry_name}</span>
          <TagList tags={config.entry_tags} limit={1} expandable={false} />
          <ArrowRight class="size-3 shrink-0 text-fg-subtle" aria-label="到" />
          {#if relays}
            <span class="whitespace-nowrap">{relays} 个中转</span>
            <ArrowRight class="size-3 shrink-0 text-fg-subtle" aria-label="到" />
          {/if}
          <span class="truncate">{config.target_name}{extra ? ` · ${extra}` : ''}</span>
          {#if !relays && config.active_path.length + config.pending_path.length > 0}<span class="text-fg-subtle">（直连）</span>{/if}
        </span>
      {:else}
        <span>外部导入</span>
        <span class="text-fg-subtle">·</span>
        <span>{protocolName(config.connection.protocol)}</span>
      {/if}
      {#if usedBy.length}
        <span class="text-fg-subtle">·</span>
        <a href={href('subscriptions')} class="whitespace-nowrap underline-offset-2 hover:text-accent-text hover:underline" title={usedBy.join('、')}>
          用于 {usedBy.length} 个订阅
        </a>
      {/if}
    </div>
    {#if config.error}
      <p class="mt-1 text-xs break-words {config.state === 'error' ? 'text-danger-text' : 'text-warn-text'}">{config.error}</p>
    {/if}
    {#if compat.text}
      <p class="mt-1 flex items-start gap-1 text-xs text-fg-muted">
        <TriangleAlert class="mt-px size-3.5 shrink-0 text-warn" />
        <span class="min-w-0 break-words">{compat.text}</span>
      </p>
    {/if}
  </div>

  <div class="flex min-w-0 items-center gap-1">
    {#if connection}
      <button
        type="button"
        onclick={copy}
        title="{address} · {plain ? '点击复制入口地址' : '点击复制分享链接（导入客户端用）'}"
        aria-label="{plain ? '复制入口地址' : '复制分享链接'} {address}"
        class="group/copy flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border border-border-strong/70 bg-surface-2 pr-2.5 pl-2.5 text-left shadow-sm transition-colors hover:border-accent hover:bg-accent-soft"
      >
        {#if !plain}
          <span class="shrink-0 rounded-sm border bg-surface px-1 text-2xs leading-4 font-medium text-fg-muted">{protocolName(connection.protocol)}</span>
        {/if}
        <code class="min-w-0 flex-1 truncate text-[0.8667rem] font-medium text-fg">{address}</code>
        <span class="flex shrink-0 items-center gap-1 text-xs font-medium {copied ? 'text-ok-text' : 'text-accent-text'}">
          {#if copied}<Check class="size-3.5" />已复制{:else}<Copy class="size-3.5" />{plain ? '复制地址' : '复制链接'}{/if}
        </span>
      </button>
    {:else if config.source === 'topology'}
      <a
        href={href(`routes/${config.topology_id}`)}
        title="链路同步到入口节点后才有可用的入口地址"
        class="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border border-dashed px-2.5 text-xs text-fg-muted transition-colors hover:border-border-strong hover:text-fg"
      >
        <Hourglass class="size-3.5 shrink-0" />
        <span class="min-w-0 flex-1 truncate">等待入口确认，生效后可复制</span>
        <span class="shrink-0 font-medium">查看链路</span>
      </a>
    {/if}
    <Button variant="ghost" size="sm" icon onclick={() => onedit(config)} aria-label="{config.source === 'topology' ? '改名' : '编辑'} {config.name}" title={config.source === 'topology' ? '改名' : '编辑'}>
      <Pencil />
    </Button>
    <Menu items={menu} label="{config.name} 的更多操作" />
  </div>
</div>
