<script lang="ts">
  import { untrack } from 'svelte';
  import { patch } from '$lib/api';
  import { FieldError, FormState } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { TopologyConfig } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import Field from '$ui/Field.svelte';
  import FormDialog from '$ui/FormDialog.svelte';
  import Input from '$ui/Input.svelte';

  type Props = {
    open?: boolean;
    /** The configuration as last loaded; follows reloads while the dialog is open. */
    config: TopologyConfig | null;
    onsaved: () => void;
    /** Reload the list, used to recover from a rename made elsewhere. */
    onrefresh: () => Promise<void>;
  };
  let { open = $bindable(false), config, onsaved, onrefresh }: Props = $props();
  const form = new FormState(['name']);
  let name = $state('');
  /** The name the edit started from; the server refuses the change if it is no longer current. */
  let previous = $state('');
  let reloading = $state(false);
  let note = $state('');

  $effect(() => {
    if (open)
      untrack(() => {
        previous = name = config?.name ?? '';
        note = '';
        form.clear();
      });
  });

  async function submit() {
    const id = config?.id;
    if (id === undefined) return;
    const ok = await form.run(() => {
      if (!name.trim()) throw new FieldError('配置名称不能为空', 'name');
      return patch(`configs/${id}/name`, { name: name.trim(), previous_name: previous });
    });
    if (!ok) return;
    open = false;
    toast.ok('配置名称已更新');
    onsaved();
  }

  /** After a conflict: take the current server name as the new base and keep what the user typed. */
  async function reload() {
    reloading = true;
    try {
      await onrefresh();
      if (!config) {
        open = false;
        toast.info('该配置已不存在');
        return;
      }
      previous = config.name;
      form.clear();
      note = `这条配置刚被改名为「${config.name}」。再次保存会用你填写的名称覆盖它。`;
    } finally {
      reloading = false;
    }
  }
</script>

<FormDialog
  bind:open
  title="配置改名"
  description={config ? `${config.code} · ${config.topology_name}：${config.entry_name} → ${config.target_name}` : undefined}
  size="sm"
  {form}
  dirty={name !== previous}
  onsubmit={submit}
>
  <Field
    label="名称"
    error={form.errors.name}
    hint={config?.connection && config.connection.protocol !== 'endpoint' ? '名称会写进分享链接和订阅，客户端里显示的就是它。' : '只用于在面板和订阅中辨认这条配置。'}
  >
    {#snippet children({ id, invalid })}
      <Input {id} {invalid} bind:value={name} maxlength={64} required autofocus onfocus={(event) => event.currentTarget.select()} />
    {/snippet}
  </Field>
  {#if note}<p class="text-xs text-fg-muted">{note}</p>{/if}
  {#snippet erroraction()}
    {#if form.status === 409}
      <Button size="sm" loading={reloading} onclick={reload}>载入最新名称</Button>
    {/if}
  {/snippet}
</FormDialog>
