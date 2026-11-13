<script lang="ts">
  import Pencil from '@lucide/svelte/icons/pencil';
  import RefreshCcw from '@lucide/svelte/icons/refresh-ccw';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import { del, errorMessage, patch } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Subscription, SubscriptionCandidate } from '$lib/types';
  import Badge from '$ui/Badge.svelte';
  import Button from '$ui/Button.svelte';
  import CopyButton from '$ui/CopyButton.svelte';
  import Menu from '$ui/Menu.svelte';
  import Switch from '$ui/Switch.svelte';
  import { compatibility } from '../configs/model';
  import { FORMATS, incompatible, memberKeys, subscriptionUrl } from './model';

  type Props = {
    subscription: Subscription;
    /** `public_url` setting, empty when unset. */
    publicUrl: string;
    /** Selectable configurations by key, for explaining what gets skipped. */
    catalog: Map<string, SubscriptionCandidate>;
    onedit: (subscription: Subscription) => void;
    onchange: () => Promise<void>;
  };
  let { subscription, publicUrl, catalog, onedit, onchange }: Props = $props();
  const uid = $props.id();

  const total = $derived(memberKeys(subscription).length);
  const skipped = $derived(incompatible(subscription, catalog));
  let busy = $state(false);
  /** The switch position while its request is in flight. */
  let wanted = $state<boolean | null>(null);
  const enabled = $derived(wanted ?? subscription.enabled);

  async function toggle(next: boolean) {
    wanted = next;
    busy = true;
    try {
      await patch(`subscriptions/${subscription.id}`, { enabled: next });
      toast.ok(next ? '订阅已启用' : '订阅已停用，链接暂时无法访问');
    } catch (err) {
      toast.error(errorMessage(err));
    }
    await onchange();
    wanted = null;
    busy = false;
  }

  async function reset() {
    const ok = await confirm({
      title: '重置订阅链接',
      body: `「${subscription.name}」的旧链接将立即失效，使用它的客户端需要重新导入新链接。`,
      confirmLabel: '重置',
      danger: true,
    });
    if (!ok) return;
    busy = true;
    try {
      await patch(`subscriptions/${subscription.id}`, { reset_token: true });
      toast.ok('链接已重置，请把新链接更新到客户端');
    } catch (err) {
      toast.error(errorMessage(err));
    }
    await onchange();
    busy = false;
  }

  async function remove() {
    const ok = await confirm({
      title: '删除订阅',
      body: `删除「${subscription.name}」？订阅链接将立即失效，其中的配置本身不受影响。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    busy = true;
    try {
      await del(`subscriptions/${subscription.id}`);
      toast.ok('订阅已删除');
    } catch (err) {
      toast.error(errorMessage(err));
    }
    await onchange();
    busy = false;
  }
</script>

<article class="rounded-lg border bg-surface shadow-sm" aria-labelledby="{uid}-name" aria-busy={busy}>
  <header class="flex items-center gap-2 border-b px-4 py-2.5">
    <div class="min-w-0 flex-1">
      <div class="flex min-w-0 items-center gap-2">
        <h2 id="{uid}-name" class="truncate text-sm font-semibold" title={subscription.name}>{subscription.name}</h2>
        {#if !enabled}<Badge tone="idle">已停用</Badge>{/if}
      </div>
      <p class="mt-0.5 text-xs text-fg-muted">
        {total} 个配置{#if total && !skipped.length}，两种格式都可用{/if}
      </p>
    </div>
    <label class="flex shrink-0 items-center gap-1.5 text-xs text-fg-muted">
      <span class="max-sm:sr-only">{enabled ? '已启用' : '已停用'}</span>
      <Switch checked={enabled} disabled={busy} label="启用订阅 {subscription.name}" onchange={toggle} />
    </label>
    <Button size="sm" onclick={() => onedit(subscription)} disabled={busy} aria-label="编辑 {subscription.name}"><Pencil />编辑</Button>
    <Menu
      label="{subscription.name} 的更多操作"
      items={[
        { label: '重置链接', icon: RefreshCcw, disabled: busy, onselect: reset },
        'separator',
        { label: '删除', icon: Trash2, danger: true, disabled: busy, onselect: remove },
      ]}
    />
  </header>

  <div class="flex flex-col gap-2.5 px-4 py-3">
    {#if !enabled}
      <p class="text-xs text-fg-muted">订阅已停用：下面的链接保持不变，但客户端访问会得到 404，重新启用后恢复。</p>
    {/if}
    {#each FORMATS as format (format.id)}
      {@const url = subscriptionUrl(publicUrl, location.origin, subscription.path, format.id)}
      {@const usable = subscription.counts[format.id]}
      <div class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1 sm:grid-cols-[7.5rem_minmax(0,1fr)_auto]">
        <label for="{uid}-{format.id}" title={format.hint}>
          <span class="text-[0.8667rem] font-medium">{format.label}</span>
          {#if total}
            <span class="tabular ml-1 text-xs {usable < total ? 'text-warn-text' : 'text-fg-subtle'}">{usable}/{total}</span>
          {/if}
        </label>
        <input
          id="{uid}-{format.id}"
          readonly
          value={url}
          spellcheck="false"
          onfocus={(event) => event.currentTarget.select()}
          class="h-8 w-full min-w-0 rounded-md border bg-surface-2 max-sm:order-3 max-sm:col-span-full px-2.5 font-mono text-xs shadow-sm focus:border-accent focus:outline-2 focus:outline-offset-0 focus:outline-(--ring) {enabled
            ? 'text-fg'
            : 'text-fg-subtle'}"
        />
        <CopyButton text={url} label="复制 {format.label} 链接" variant="secondary" size="md" class="sm:w-40" />
      </div>
    {/each}

    {#if !total}
      <p class="flex items-start gap-1.5 text-xs text-warn-text">
        <TriangleAlert class="mt-px size-3.5 shrink-0" />还没有绑定配置，客户端拉取到的订阅是空的。
        <button type="button" class="font-medium underline underline-offset-2" onclick={() => onedit(subscription)}>去选择</button>
      </p>
    {:else if skipped.length}
      <details class="group/skip text-xs">
        <summary class="flex cursor-pointer items-center gap-1.5 text-warn-text select-none">
          <TriangleAlert class="size-3.5 shrink-0" />
          {skipped.length} 个配置在部分或全部格式中会被跳过
          <span class="text-fg-muted underline underline-offset-2 group-open/skip:hidden">查看原因</span>
        </summary>
        <ul class="mt-1.5 flex flex-col gap-1 border-l-2 pl-3 text-fg-muted">
          {#each skipped as item (item.source + item.id)}
            <li class="break-words">
              <code class="text-2xs">{item.code}</code>
              <span class="font-medium text-fg">{item.name}</span>
              — {compatibility(item).text}
            </li>
          {/each}
        </ul>
      </details>
    {/if}
  </div>
</article>
