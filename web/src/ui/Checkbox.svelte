<script lang="ts">
  import type { Snippet } from 'svelte';
  type Props = {
    checked?: boolean;
    indeterminate?: boolean;
    disabled?: boolean;
    onchange?: (checked: boolean) => void;
    children?: Snippet;
    class?: string;
  };
  let { checked = $bindable(false), indeterminate = false, disabled = false, onchange, children, class: klass = '' }: Props = $props();
</script>

<label class="inline-flex items-center gap-2 text-sm {disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'} {klass}">
  <input
    type="checkbox"
    bind:checked
    {indeterminate}
    {disabled}
    onchange={() => onchange?.(checked)}
    class="size-3.5 shrink-0 rounded-[4px] accent-(--accent)"
  />
  {@render children?.()}
</label>
