<script lang="ts" generics="T extends string | number">
  import type { HTMLSelectAttributes } from 'svelte/elements';

  type Option = { value: T; label: string; disabled?: boolean };
  type Props = { value?: T; options: Option[]; invalid?: boolean } & Omit<HTMLSelectAttributes, 'value'>;
  let { value = $bindable(), options, invalid = false, class: klass = '', ...rest }: Props = $props();
</script>

<select
  bind:value
  aria-invalid={invalid || undefined}
  class="select-chevron h-8 min-w-0 appearance-none rounded-md border bg-surface pr-7 pl-2.5 text-sm text-fg shadow-sm transition-colors hover:border-border-strong focus:border-accent focus:outline-2 focus:outline-offset-0 focus:outline-(--ring) disabled:bg-surface-2 disabled:text-fg-muted {invalid
    ? 'border-danger'
    : 'border-border-strong/70'} {klass}"
  {...rest}
>
  {#each options as option (option.value)}
    <option value={option.value} disabled={option.disabled}>{option.label}</option>
  {/each}
</select>
