<script lang="ts">
  import type { Snippet } from 'svelte';
  import { place, type Side } from '$lib/floating';

  type Props = { text?: string; content?: Snippet; side?: Side; children: Snippet; class?: string };
  let { text, content, side = 'top', children, class: klass = '' }: Props = $props();
  const id = $props.id();
  let anchor = $state<HTMLSpanElement>();
  let shown = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;

  function show(delay: number) {
    if (!text && !content) return;
    clearTimeout(timer);
    timer = setTimeout(() => (shown = true), delay);
  }
  function hide() {
    clearTimeout(timer);
    shown = false;
  }
  // The tip lives in the top layer so it is never clipped and also shows above modal dialogs.
  function mounted(tip: HTMLElement) {
    tip.showPopover();
    if (anchor) place(anchor.getBoundingClientRect(), tip, side, 'center', 6);
  }
</script>

<svelte:window onscroll={hide} />

<span
  bind:this={anchor}
  class="inline-flex min-w-0 items-center {klass}"
  aria-describedby={shown ? id : undefined}
  onpointerenter={(event) => event.pointerType !== 'touch' && show(250)}
  onpointerleave={hide}
  onpointerdown={hide}
  onfocusin={(event) => (event.target as HTMLElement).matches(':focus-visible') && show(0)}
  onfocusout={hide}
  role="presentation"
>
  {@render children()}
</span>
{#if shown}
  <div
    {@attach mounted}
    {id}
    popover="manual"
    role="tooltip"
    class="pointer-events-none fixed inset-auto m-0 max-w-xs animate-fade rounded-md bg-fg px-2 py-1 text-xs leading-relaxed break-words text-bg shadow-md"
  >
    {#if content}{@render content()}{:else}{text}{/if}
  </div>
{/if}
