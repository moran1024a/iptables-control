<script lang="ts">
  import { confirmState } from '$lib/confirm.svelte';
  import Button from './Button.svelte';
  import Dialog from './Dialog.svelte';

  let open = $derived(confirmState.current !== null);
</script>

{#if confirmState.current}
  {@const request = confirmState.current}
  <Dialog {open} title={request.title} size="sm" onclose={() => confirmState.answer(false)}>
    {#if request.body}<p class="text-sm leading-relaxed whitespace-pre-line text-fg-muted">{request.body}</p>{/if}
    {#if request.detail}
      <pre class="mt-3 max-h-40 overflow-auto rounded-md bg-surface-2 p-2.5 text-xs break-all whitespace-pre-wrap">{request.detail}</pre>
    {/if}
    {#snippet footer()}
      <Button onclick={() => confirmState.answer(false)}>{request.cancelLabel ?? '取消'}</Button>
      <Button variant={request.danger ? 'danger' : 'primary'} onclick={() => confirmState.answer(true)} autofocus>
        {request.confirmLabel ?? '确定'}
      </Button>
    {/snippet}
  </Dialog>
{/if}
