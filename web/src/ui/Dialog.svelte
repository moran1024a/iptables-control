<script lang="ts">
  import X from '@lucide/svelte/icons/x';
  import { untrack, type Snippet } from 'svelte';
  import { overlays } from '$lib/overlay.svelte';

  type Props = {
    open?: boolean;
    title: string;
    description?: string;
    size?: 'sm' | 'md' | 'lg' | 'xl';
    /** Slide in from the right instead of centring; for detail and long forms. */
    side?: boolean;
    /** Keep the dialog open on backdrop clicks, for forms with unsaved input. */
    persistent?: boolean;
    onclose?: () => void;
    /** Return false to keep the dialog open, e.g. while saving or after declining to discard changes. */
    beforeclose?: () => boolean | Promise<boolean>;
    children: Snippet;
    footer?: Snippet;
  };
  let {
    open = $bindable(false),
    title,
    description,
    size = 'md',
    side = false,
    persistent = false,
    onclose,
    beforeclose,
    children,
    footer,
  }: Props = $props();
  const widths = { sm: 'sm:max-w-sm', md: 'sm:max-w-lg', lg: 'sm:max-w-2xl', xl: 'sm:max-w-4xl' };
  const id = $props.id();
  let el = $state<HTMLDialogElement>();
  let pressedBackdrop = false;

  // The native modal dialog provides the focus trap, Escape, inert background and top layer.
  $effect(() => {
    if (!el) return;
    if (open && !el.open) el.showModal();
    else if (!open && el.open) el.close();
  });

  // Every open dialog pauses background polling.
  $effect(() => {
    if (!open) return;
    // untrack: the counter is written here, so reading it must not re-run this effect.
    untrack(() => overlays.count++);
    return () => {
      untrack(() => overlays.count--);
    };
  });

  function closed() {
    if (open) {
      open = false;
      onclose?.();
    }
  }

  let asking = false;
  async function requestClose() {
    if (asking) return;
    asking = true;
    try {
      if (!beforeclose || (await beforeclose())) el?.close();
    } finally {
      asking = false;
    }
  }
</script>

<dialog
  bind:this={el}
  aria-labelledby="{id}-title"
  aria-describedby={description ? `${id}-desc` : undefined}
  onclose={closed}
  oncancel={(event) => {
    // Escape: route through the same guard as the close button.
    event.preventDefault();
    void requestClose();
  }}
  onpointerdown={(event) => (pressedBackdrop = event.target === el)}
  onclick={(event) => {
    // Only a press that both starts and ends on the backdrop closes; a text selection dragged outside must not.
    if (event.target === el && pressedBackdrop && !persistent) void requestClose();
  }}
  class="m-0 max-h-none max-w-none bg-transparent p-0 text-fg backdrop:bg-black/45 backdrop:backdrop-blur-[2px] open:flex {side
    ? 'fixed inset-y-0 right-0 left-auto h-dvh w-full justify-end'
    : 'fixed inset-0 size-full items-center justify-center p-4'}"
>
  <div
    class="flex w-full flex-col border bg-surface shadow-lg {side
      ? `h-dvh animate-slide sm:border-y-0 sm:border-r-0 ${widths[size]}`
      : `max-h-[min(88dvh,56rem)] animate-in rounded-xl ${widths[size]}`}"
  >
    <header class="flex shrink-0 items-start justify-between gap-4 border-b px-5 py-3.5">
      <div class="min-w-0">
        <h2 id="{id}-title" class="truncate text-[0.9375rem] font-semibold">{title}</h2>
        {#if description}<p id="{id}-desc" class="mt-0.5 text-[0.8667rem] text-fg-muted">{description}</p>{/if}
      </div>
      <button
        type="button"
        onclick={requestClose}
        class="-mr-1.5 flex size-7 shrink-0 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3 hover:text-fg"
        aria-label="关闭"
      >
        <X class="size-4" />
      </button>
    </header>
    <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4">
      {#if open}{@render children()}{/if}
    </div>
    {#if footer}
      <footer class="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t bg-surface-2/60 px-5 py-3 {side ? '' : 'rounded-b-xl'}">
        {@render footer()}
      </footer>
    {/if}
  </div>
</dialog>
