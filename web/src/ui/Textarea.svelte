<script lang="ts">
  import type { HTMLTextareaAttributes } from 'svelte/elements';

  type Props = { value?: string; invalid?: boolean; mono?: boolean } & Omit<HTMLTextareaAttributes, 'value'>;
  let { value = $bindable(''), invalid = false, mono = false, class: klass = '', rows = 3, ...rest }: Props = $props();
</script>

<textarea
  bind:value
  {rows}
  aria-invalid={invalid || undefined}
  class="w-full min-w-0 resize-y rounded-md border bg-surface px-2.5 py-1.5 text-sm text-fg shadow-sm transition-colors placeholder:text-fg-subtle hover:border-border-strong focus:border-accent focus:outline-2 focus:outline-offset-0 focus:outline-(--ring) {invalid
    ? 'border-danger'
    : 'border-border-strong/70'} {mono ? 'font-mono text-[0.8667rem] break-all' : ''} {klass}"
  {...rest}
></textarea>
