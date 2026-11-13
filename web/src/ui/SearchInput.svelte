<script lang="ts">
  import Search from '@lucide/svelte/icons/search';
  import X from '@lucide/svelte/icons/x';

  type Props = { value?: string; placeholder?: string; class?: string; label?: string };
  let { value = $bindable(''), placeholder = '搜索', class: klass = 'w-56', label }: Props = $props();
</script>

<div class="relative {klass}">
  <Search class="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-fg-subtle" />
  <input
    type="text"
    inputmode="search"
    bind:value
    {placeholder}
    aria-label={label ?? placeholder}
    onkeydown={(event) => event.key === 'Escape' && value && ((value = ''), event.stopPropagation())}
    class="h-8 w-full rounded-md border border-border-strong/70 bg-surface pr-7 pl-8 text-sm shadow-sm transition-colors placeholder:text-fg-subtle hover:border-border-strong focus:border-accent focus:outline-2 focus:outline-offset-0 focus:outline-(--ring)"
  />
  {#if value}
    <button
      type="button"
      aria-label="清除搜索"
      onclick={() => (value = '')}
      class="absolute top-1/2 right-1.5 flex size-5 -translate-y-1/2 items-center justify-center rounded text-fg-subtle hover:bg-surface-3 hover:text-fg"
    >
      <X class="size-3.5" />
    </button>
  {/if}
</div>
