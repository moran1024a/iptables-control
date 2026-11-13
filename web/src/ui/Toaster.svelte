<script lang="ts">
  import CircleAlert from '@lucide/svelte/icons/circle-alert';
  import CircleCheck from '@lucide/svelte/icons/circle-check';
  import Info from '@lucide/svelte/icons/info';
  import { toasts } from '$lib/toast.svelte';

  let host = $state<HTMLDivElement>();
  // Re-enter the top layer on every change so toasts stay above any modal dialog opened earlier.
  $effect(() => {
    if (!host) return;
    if (host.matches(':popover-open')) host.hidePopover();
    if (toasts.items.length) host.showPopover();
  });
</script>

<div
  bind:this={host}
  popover="manual"
  class="pointer-events-none fixed inset-x-0 top-auto bottom-18 m-0 lg:bottom-4 w-auto flex-col items-center gap-2 bg-transparent px-4 open:flex"
  aria-live="polite"
>
  {#each toasts.items as item (item.id)}
    <button
      type="button"
      onclick={() => toasts.dismiss(item.id)}
      class="pointer-events-auto flex max-w-md animate-in items-start gap-2 rounded-lg border bg-surface px-3 py-2 text-left text-sm shadow-md"
      role={item.tone === 'error' ? 'alert' : 'status'}
    >
      {#if item.tone === 'ok'}<CircleCheck class="mt-0.5 size-4 shrink-0 text-ok" />
      {:else if item.tone === 'error'}<CircleAlert class="mt-0.5 size-4 shrink-0 text-danger" />
      {:else}<Info class="mt-0.5 size-4 shrink-0 text-info" />{/if}
      <span class="break-words">{item.text}</span>
    </button>
  {/each}
</div>
