<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    label?: string;
    hint?: string;
    error?: string | null;
    optional?: boolean;
    class?: string;
    children: Snippet<[{ id: string; invalid: boolean }]>;
    /** Extra content on the label row, e.g. a small action. */
    aside?: Snippet;
  };
  let { label, hint, error, optional = false, class: klass = '', children, aside }: Props = $props();
  const id = $props.id();
</script>

<div class="flex min-w-0 flex-col gap-1 {klass}">
  {#if label || aside}
    <div class="flex items-baseline justify-between gap-2">
      {#if label}
        <label for={id} class="text-[0.8667rem] font-medium text-fg">
          {label}{#if optional}<span class="ml-1 font-normal text-fg-subtle">可选</span>{/if}
        </label>
      {/if}
      {@render aside?.()}
    </div>
  {/if}
  {@render children({ id, invalid: !!error })}
  {#if error}
    <p class="text-xs text-danger-text" role="alert">{error}</p>
  {:else if hint}
    <p class="text-xs leading-relaxed text-fg-muted">{hint}</p>
  {/if}
</div>
