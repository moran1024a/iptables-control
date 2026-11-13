<script lang="ts">
  import { LINK_STATE } from '$lib/status';
  import Badge from './Badge.svelte';

  let { state, title, class: klass = '' }: { state: string; title?: string; class?: string } = $props();
  const info = $derived(LINK_STATE[state]);
</script>

<Badge tone={info?.tone ?? 'warn'} dot pulse={info?.live} {title} class={klass}>{info?.label ?? state}</Badge>
