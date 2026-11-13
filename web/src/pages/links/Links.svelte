<script lang="ts">
  import { router } from '$lib/router.svelte';
  import LinkDetail from './LinkDetail.svelte';
  import LinkList from './LinkList.svelte';
  import Quality from './Quality.svelte';

  // #/routes · #/routes/new · #/routes/<id> · #/routes/<id>/quality
  const id = $derived(router.id(1));
  const creating = $derived(router.parts[1] === 'new');
  const quality = $derived(!!id && router.parts[2] === 'quality');
</script>

{#if creating}
  <LinkDetail id={null} />
{:else if id && quality}
  {#key id}<Quality {id} />{/key}
{:else if id}
  {#key id}<LinkDetail {id} />{/key}
{:else}
  <LinkList />
{/if}
