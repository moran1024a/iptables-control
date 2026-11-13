<script lang="ts">
  import Check from '@lucide/svelte/icons/check';
  import Copy from '@lucide/svelte/icons/copy';
  import { copyText } from '$lib/clipboard';
  import Button from './Button.svelte';

  type Props = { text: string | (() => string | Promise<string>); label?: string; size?: 'xs' | 'sm' | 'md'; variant?: 'secondary' | 'ghost' | 'primary'; class?: string };
  let { text, label, size = 'sm', variant = 'ghost', class: klass = '' }: Props = $props();
  let done = $state(false);
  let timer: ReturnType<typeof setTimeout>;

  async function copy() {
    const value = typeof text === 'function' ? await text() : text;
    if (await copyText(value)) {
      done = true;
      clearTimeout(timer);
      timer = setTimeout(() => (done = false), 1500);
    }
  }
</script>

<Button {size} {variant} icon={!label} class={klass} onclick={copy} aria-label={label ? undefined : '复制'} title={label ? undefined : '复制'}>
  {#if done}<Check class="text-ok" />{:else}<Copy />{/if}
  {#if label}{done ? '已复制' : label}{/if}
</Button>
