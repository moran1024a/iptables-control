<script lang="ts" module>
  // Open sections are remembered for this tab, so a background refresh or a return visit keeps them open.
  const remembered = new Set<string>();
</script>

<script lang="ts">
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import type { Snippet } from 'svelte';

  type Props = { title: string; hint?: string; /** Unique key to remember the open state. */ key?: string; open?: boolean; children: Snippet; class?: string; card?: boolean };
  let { title, hint, key, open = $bindable(false), children, class: klass = '', card = true }: Props = $props();
  // svelte-ignore state_referenced_locally
  if (key && remembered.has(key)) open = true;
  const id = $props.id();

  function toggle() {
    open = !open;
    if (key) open ? remembered.add(key) : remembered.delete(key);
  }
</script>

<section class="{card ? 'rounded-lg border bg-surface shadow-sm' : ''} {klass}">
  <button
    type="button"
    aria-expanded={open}
    aria-controls={id}
    onclick={toggle}
    class="flex min-h-11 w-full items-center gap-2 px-4 py-2 text-left"
  >
    <ChevronDown class="size-4 shrink-0 text-fg-subtle transition-transform {open ? '' : '-rotate-90'}" />
    <span class="text-[0.8125rem] font-semibold">{title}</span>
    {#if hint}<span class="truncate text-xs text-fg-muted">{hint}</span>{/if}
  </button>
  {#if open}
    <div {id} class="{card ? 'border-t p-4' : 'pb-2'}">{@render children()}</div>
  {/if}
</section>
