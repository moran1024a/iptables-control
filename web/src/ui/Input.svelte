<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  type Props = { value?: string | number | null; invalid?: boolean; mono?: boolean; el?: HTMLInputElement } & Omit<
    HTMLInputAttributes,
    'value'
  >;
  let { value = $bindable(''), invalid = false, mono = false, el = $bindable(), class: klass = '', ...rest }: Props = $props();
</script>

<input
  bind:this={el}
  bind:value
  aria-invalid={invalid || undefined}
  class="h-8 w-full min-w-0 rounded-md border bg-surface px-2.5 text-sm text-fg shadow-sm transition-colors placeholder:text-fg-subtle hover:border-border-strong focus:border-accent focus:outline-2 focus:outline-offset-0 focus:outline-(--ring) disabled:bg-surface-2 disabled:text-fg-muted {invalid
    ? 'border-danger'
    : 'border-border-strong/70'} {mono ? 'font-mono text-[0.8667rem]' : ''} {klass}"
  {...rest}
/>
