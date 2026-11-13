<script lang="ts">
  import { router } from '$lib/router.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import NodeList from './NodeList.svelte';

  const id = $derived(router.id(1));
  // The detail page is larger than the list and not part of the first paint.
  const detail = () => import('./NodeDetail.svelte');
</script>

{#if id}
  {#await detail()}
    <div class="flex justify-center py-24 text-fg-subtle"><Spinner class="size-5" /></div>
  {:then module}
    {#key id}<module.default {id} />{/key}
  {/await}
{:else}
  <NodeList />
{/if}
