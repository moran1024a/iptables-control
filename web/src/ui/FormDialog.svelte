<script lang="ts">
  import type { Snippet } from 'svelte';
  import { confirm } from '$lib/confirm.svelte';
  import type { FormState } from '$lib/form.svelte';
  import Alert from './Alert.svelte';
  import Button from './Button.svelte';
  import Dialog from './Dialog.svelte';

  type Props = {
    open?: boolean;
    title: string;
    description?: string;
    size?: 'sm' | 'md' | 'lg' | 'xl';
    side?: boolean;
    form: FormState;
    /** True when the user has changed something; closing then asks before discarding. */
    dirty?: boolean;
    submitLabel?: string;
    cancelLabel?: string;
    submitDisabled?: boolean;
    danger?: boolean;
    onsubmit: () => void | Promise<void>;
    onclose?: () => void;
    children: Snippet;
    /** Extra buttons on the left side of the footer. */
    extra?: Snippet;
    /** Shown with the form-level error, e.g. a "reload" action after a conflict. */
    erroraction?: Snippet;
  };
  let {
    open = $bindable(false),
    title,
    description,
    size = 'md',
    side = false,
    form,
    dirty = false,
    submitLabel = '保存',
    cancelLabel = '取消',
    submitDisabled = false,
    danger = false,
    onsubmit,
    onclose,
    children,
    extra,
    erroraction,
  }: Props = $props();
  const id = $props.id();

  async function beforeclose() {
    if (form.busy) return false;
    if (!dirty) return true;
    return confirm({ title: '放弃未保存的修改？', confirmLabel: '放弃修改', cancelLabel: '继续编辑', danger: true });
  }

  async function close() {
    if (await beforeclose()) open = false;
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!form.busy) void onsubmit();
  }
</script>

<Dialog bind:open {title} {description} {size} {side} persistent {beforeclose} {onclose}>
  <form id="{id}-form" onsubmit={submit} class="flex flex-col gap-4" inert={form.busy} aria-busy={form.busy} novalidate={false}>
    {@render children()}
    {#if form.error}
      <Alert tone="danger" actions={erroraction}>{form.error}</Alert>
    {/if}
  </form>
  {#snippet footer()}
    {#if extra}<div class="mr-auto flex items-center gap-2">{@render extra()}</div>{/if}
    <Button onclick={close} disabled={form.busy}>{cancelLabel}</Button>
    <Button type="submit" form="{id}-form" variant={danger ? 'danger' : 'primary'} loading={form.busy} disabled={submitDisabled}>
      {form.busy ? '处理中…' : submitLabel}
    </Button>
  {/snippet}
</Dialog>
