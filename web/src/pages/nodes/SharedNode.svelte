<script lang="ts">
  import { get } from '$lib/api';
  import { gigabytes } from '$lib/format';
  import { usePoll } from '$lib/poll.svelte';
  import { MODE_LABEL } from '$lib/status';
  import type { NodeSummary } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Card from '$ui/Card.svelte';
  import NodeStatusBadge from '$ui/NodeStatusBadge.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import StatusBar from '$ui/StatusBar.svelte';
  import TagList from '$ui/TagList.svelte';
  import Page from '../../layout/Page.svelte';

  let { id }: { id: number } = $props();
  const poll = usePoll((signal) => get<{ node: NodeSummary }>(`nodes/${id}`, { signal }), { interval: 5000 });
  const node = $derived(poll.error ? undefined : poll.data?.node);
</script>

<Page title={node?.name ?? '节点'} back="nodes" backLabel="节点">
  {#snippet meta()}
    {#if node}<NodeStatusBadge status={node.status} /><TagList tags={node.tags} />{/if}
  {/snippet}
  {#if poll.error}
    <Alert tone="warn">{poll.error}</Alert>
  {:else if node}
    <Card title={node.addresses_hidden ? '节点状态' : '连接地址'} hint={MODE_LABEL[node.desired_mode] + '节点'}>
      <StatusBar bar={node.bar} />
      {#if node.addresses_hidden}
        <p class="mt-2 text-xs text-fg-muted">地址已隐藏 · 仅中转</p>
      {:else}
      <ul class="mt-3 divide-y">
        {#each node.addresses as address (address.id)}
          <li class="flex flex-wrap items-center gap-2 py-2 text-sm">
            <span>{address.name || (address.primary ? '主地址' : '副地址')}</span>
            <code class="ml-auto break-all">{address.host}</code>
          </li>
        {/each}
      </ul>
      {/if}
    </Card>
    {#if node.traffic_quota}
      {@const quota = node.traffic_quota}
      <Card title="转发流量" class="mt-4">
        <p class="text-sm">已用 {gigabytes(quota.used)} · {quota.remaining === null ? '不限额' : `剩余 ${gigabytes(quota.remaining)}`}</p>
        {#if quota.exhausted}<p class="mt-1 text-xs text-warn-text">此节点的流量额度已用完。</p>{/if}
      </Card>
    {/if}
  {:else}
    <div class="flex justify-center py-16"><Spinner class="size-5" /></div>
  {/if}
</Page>
