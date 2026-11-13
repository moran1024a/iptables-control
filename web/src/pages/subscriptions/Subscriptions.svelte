<script lang="ts">
  import KeyRound from '@lucide/svelte/icons/key-round';
  import Plus from '@lucide/svelte/icons/plus';
  import Rss from '@lucide/svelte/icons/rss';
  import { get } from '$lib/api';
  import { usePoll } from '$lib/poll.svelte';
  import { href } from '$lib/router.svelte';
  import type { Subscription, SubscriptionCandidate, SubscriptionsList } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import EmptyState from '$ui/EmptyState.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Page from '../../layout/Page.svelte';
  import { configKey } from '../configs/model';
  import SubscriptionCard from './SubscriptionCard.svelte';
  import SubscriptionDialog from './SubscriptionDialog.svelte';

  const list = usePoll((signal) => get<SubscriptionsList>('subscriptions', { signal }), { interval: 5000 });
  const data = $derived(list.data);
  const subscriptions = $derived(data?.subscriptions ?? []);
  const catalog = $derived(new Map<string, SubscriptionCandidate>((data?.configs ?? []).map((item) => [configKey(item), item])));

  let dialogOpen = $state(false);
  let editingId = $state<number | null>(null);
  const editing = $derived(subscriptions.find((item) => item.id === editingId) ?? null);

  // Polling stops while the dialog is open, so load the current catalog right before showing it.
  async function openDialog(subscription: Subscription | null) {
    await list.refresh();
    editingId = subscription?.id ?? null;
    dialogOpen = true;
  }
</script>

<Page title="订阅" subtitle="把多条配置打包成一个链接，客户端定期拉取就能跟随面板更新。">
  {#snippet meta()}
    {#if subscriptions.length}<span class="tabular text-sm text-fg-subtle">{subscriptions.length}</span>{/if}
  {/snippet}
  {#snippet actions()}
    {#if data}<Button variant="primary" onclick={() => openDialog(null)}><Plus />新增订阅</Button>{/if}
  {/snippet}

  {#if list.error && data}
    <Alert tone="warn">刷新失败：{list.error}。显示的是上一次的数据。</Alert>
  {/if}
  {#if !data}
    {#if list.error}
      <Alert tone="danger">{list.error}</Alert>
    {:else}
      <div class="flex justify-center py-16 text-fg-subtle"><Spinner class="size-5" /></div>
    {/if}
  {:else if !subscriptions.length}
    <div class="rounded-lg border border-dashed bg-surface">
      <EmptyState
        icon={Rss}
        title="还没有订阅"
        text={data.configs.length
          ? '选择要下发的配置生成一个订阅链接，填进 Mihomo 或 v2rayN 即可；之后增减配置不用再改客户端。'
          : '订阅用来把配置下发给客户端。目前还没有任何配置，可以先创建链路或导入外部配置。'}
      >
        <Button variant="primary" onclick={() => openDialog(null)}><Plus />新增订阅</Button>
        {#if !data.configs.length}<Button href={href('configs')}>查看配置</Button>{/if}
      </EmptyState>
    </div>
  {:else}
    {#if !data.public_url}
      <Alert tone="info">
        链接使用的是你当前访问面板的地址 <code class="font-medium break-all">{location.origin}</code>。如果客户端无法访问这个地址（例如内网或本机地址），请先在
        <a class="font-medium underline underline-offset-2" href={href('settings')}>设置</a> 里填写「对外地址」。
      </Alert>
    {/if}
    <p class="flex items-start gap-1.5 px-1 text-xs leading-relaxed text-fg-muted">
      <KeyRound class="mt-0.5 size-3.5 shrink-0" />
      <span>
        订阅链接本身就是访问凭据：拿到链接的人可以获取其中全部节点信息，请勿公开。泄露后用「重置链接」作废旧链接。{#if data.public_url}链接使用<a
            class="underline underline-offset-2 hover:text-fg"
            href={href('settings')}>设置</a
          >中的对外地址 <code>{data.public_url}</code>。{/if}
      </span>
    </p>
    <div class="flex flex-col gap-3">
      {#each subscriptions as subscription (subscription.id)}
        <SubscriptionCard {subscription} publicUrl={data.public_url} {catalog} onedit={openDialog} onchange={list.refresh} />
      {/each}
    </div>
  {/if}
</Page>

{#if data}
  <SubscriptionDialog bind:open={dialogOpen} subscription={editing} catalog={data.configs} groups={data.groups} onsaved={list.refresh} />
{/if}
