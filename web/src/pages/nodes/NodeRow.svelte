<script lang="ts">
  import { ago, signed } from '$lib/format';
  import { href } from '$lib/router.svelte';
  import { MODE_LABEL } from '$lib/status';
  import type { NodeBar, NodeBrief } from '$lib/types';
  import NodeStatusBadge from '$ui/NodeStatusBadge.svelte';
  import StatusBar from '$ui/StatusBar.svelte';
  import TagList from '$ui/TagList.svelte';

  let { node, now }: { node: NodeBrief & { bar: NodeBar }; now: number } = $props();
  const extra = $derived(node.addresses.filter((address) => !address.primary));
  const note = $derived(node.note.replace(/\s*\n\s*/g, ' '));
</script>

<a
  href={href(`nodes/${node.id}`)}
  class="group grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1.5 px-4 py-2.5 transition-colors hover:bg-surface-2 md:grid-cols-[minmax(0,1fr)_12rem_15rem]"
>
  <div class="min-w-0">
    <div class="flex min-w-0 items-center gap-2">
      <span
        class="shrink-0 rounded-sm border px-1 text-2xs leading-4 font-medium {node.desired_mode === 'active'
          ? 'border-accent/30 text-accent-text'
          : 'text-fg-muted'}"
      >
        {MODE_LABEL[node.desired_mode]}
      </span>
      <span class="truncate text-sm font-medium group-hover:text-accent-text" title={node.name}>{node.name}</span>
      <TagList tags={node.tags} />
    </div>
    <div class="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-fg-muted">
      <code class="shrink-0 text-fg-muted">{node.addr}</code>
      {#if extra.length}
        <span class="shrink-0 rounded-sm bg-surface-3 px-1 text-2xs" title={extra.map((address) => address.host).join('、')}>＋{extra.length} IP</span>
      {/if}
      {#if note}<span class="truncate" title={node.note}>· {note}</span>{/if}
    </div>
  </div>
  <StatusBar bar={node.bar} size="xs" class="hidden md:block" />
  <div class="flex items-center justify-end gap-3 max-sm:flex-col max-sm:items-end max-sm:gap-1">
    {#if node.weight !== 0}
      <span class="tabular mr-auto text-xs text-fg-muted max-md:hidden" title="全局权重，越高越优先">权重 <b class="font-semibold text-fg">{signed(node.weight)}</b></span>
    {/if}
    <NodeStatusBadge status={node.status} />
    <span class="tabular w-16 text-right text-xs text-fg-subtle">{ago(node.last_seen, now)}</span>
  </div>
  {#if node.wake?.error}
    <p class="col-span-full text-xs text-warn-text">{node.wake.error}</p>
  {/if}
</a>
