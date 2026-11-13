<script lang="ts" generics="T extends string | number">
  type Props = { value: T; options: { value: T; label: string; count?: number }[]; onchange?: (value: T) => void; label?: string; class?: string };
  let { value = $bindable(), options, onchange, label, class: klass = '' }: Props = $props();
</script>

<div role="tablist" aria-label={label} class="inline-flex h-8 items-center gap-0.5 rounded-lg bg-surface-3 p-0.5 {klass}">
  {#each options as option (option.value)}
    <button
      type="button"
      role="tab"
      aria-selected={value === option.value}
      onclick={() => {
        value = option.value;
        onchange?.(option.value);
      }}
      class="inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[0.8125rem] font-medium whitespace-nowrap transition-colors {value === option.value
        ? 'bg-surface text-fg shadow-sm'
        : 'text-fg-muted hover:text-fg'}"
    >
      {option.label}
      {#if option.count !== undefined}<span class="tabular text-xs text-fg-subtle">{option.count}</span>{/if}
    </button>
  {/each}
</div>
