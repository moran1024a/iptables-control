<script lang="ts">
  import type { Snippet } from 'svelte';
  type Props = { title?: string; hint?: string; actions?: Snippet; children: Snippet; class?: string; flush?: boolean };
  /** A titled panel. `flush` removes the body padding for tables and row lists. */
  let { title, hint, actions, children, class: klass = '', flush = false }: Props = $props();
</script>

<section class="rounded-lg border bg-surface shadow-sm {klass}">
  {#if title || actions}
    <header class="flex min-h-11 flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
      <div class="flex min-w-0 items-baseline gap-2">
        {#if title}<h2 class="text-[0.8667rem] font-semibold">{title}</h2>{/if}
        {#if hint}<span class="truncate text-xs text-fg-muted">{hint}</span>{/if}
      </div>
      {#if actions}<div class="flex items-center gap-1.5">{@render actions()}</div>{/if}
    </header>
  {/if}
  <div class={flush ? '' : 'p-4'}>{@render children()}</div>
</section>
