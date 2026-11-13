<script lang="ts">
  type Props = { checked?: boolean; disabled?: boolean; label?: string; onchange?: (checked: boolean) => void; id?: string };
  let { checked = $bindable(false), disabled = false, label, onchange, id }: Props = $props();
  function toggle() {
    checked = !checked;
    onchange?.(checked);
  }
</script>

<button
  {id}
  type="button"
  role="switch"
  aria-checked={checked}
  aria-label={label}
  {disabled}
  onclick={toggle}
  class="relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full transition-colors duration-150 disabled:opacity-50 {checked
    ? 'bg-accent'
    : 'bg-border-strong'}"
>
  <span
    class="size-3.5 rounded-full bg-white shadow-sm transition-transform duration-150 {checked ? 'translate-x-4' : 'translate-x-0.5'}"
  ></span>
</button>
