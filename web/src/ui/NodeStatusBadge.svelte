<script lang="ts">
  import { NODE_STATUS } from '$lib/status';
  import type { NodeStatus } from '$lib/types';
  import Badge from './Badge.svelte';

  let { status, class: klass = '' }: { status: NodeStatus | string; class?: string } = $props();
  const info = $derived(NODE_STATUS[status as NodeStatus]);
</script>

<Badge tone={info?.tone ?? 'idle'} dot pulse={info?.live} class={klass}>{info?.label ?? status}</Badge>
