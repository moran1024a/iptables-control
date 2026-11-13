<script lang="ts">
  import CopyButton from '$ui/CopyButton.svelte';

  type Props = {
    /** Text on screen; may be masked. */
    text: string;
    /** Text that is copied, when it differs from what is shown. */
    copy?: string;
    /** Prefix a shell prompt. */
    prompt?: boolean;
    copyLabel?: string;
    primary?: boolean;
    class?: string;
  };
  let { text, copy, prompt = false, copyLabel, primary = false, class: klass = '' }: Props = $props();
</script>

<div class="flex min-w-0 items-start gap-1.5 {klass}">
  <code class="block min-w-0 flex-1 rounded-md border bg-surface-2 px-2.5 py-1.5 font-mono text-xs leading-relaxed break-all text-fg select-all">
    {#if prompt}<span class="mr-[1ch] text-fg-subtle select-none">$</span>{/if}{text}
  </code>
  <CopyButton text={copy ?? text} label={copyLabel} variant={primary ? 'primary' : 'ghost'} size={primary ? 'md' : 'sm'} class="mt-0.5" />
</div>
