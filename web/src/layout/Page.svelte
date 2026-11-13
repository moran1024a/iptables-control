<script lang="ts">
  import ChevronLeft from '@lucide/svelte/icons/chevron-left';
  import type { Snippet } from 'svelte';

  type Props = {
    title: string;
    subtitle?: string;
    /** Hash path of the parent list; renders a back link. */
    back?: string;
    backLabel?: string;
    wide?: boolean;
    actions?: Snippet;
    /** Rendered next to the title, e.g. a status badge. */
    meta?: Snippet;
    toolbar?: Snippet;
    children: Snippet;
  };
  let { title, subtitle, back, backLabel = '返回', wide = false, actions, meta, toolbar, children }: Props = $props();
</script>

<div class="mx-auto flex w-full flex-col gap-4 px-4 py-5 sm:px-6 lg:px-8 lg:py-7 {wide ? 'max-w-[110rem]' : 'max-w-6xl'}">
  <header class="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
    <div class="min-w-0">
      {#if back}
        <a href={'#/' + back} class="-ml-1 mb-1 inline-flex items-center gap-0.5 text-xs font-medium text-fg-muted hover:text-fg">
          <ChevronLeft class="size-3.5" />{backLabel}
        </a>
      {/if}
      <div class="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
        <h1 class="truncate text-xl leading-7 font-semibold tracking-tight">{title}</h1>
        {@render meta?.()}
      </div>
      {#if subtitle}<p class="mt-0.5 text-[0.8667rem] text-fg-muted">{subtitle}</p>{/if}
    </div>
    {#if actions}<div class="flex flex-wrap items-center gap-2">{@render actions()}</div>{/if}
  </header>
  {#if toolbar}<div class="flex flex-wrap items-center gap-2">{@render toolbar()}</div>{/if}
  {@render children()}
</div>
