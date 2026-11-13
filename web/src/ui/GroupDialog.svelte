<script lang="ts">
  import { patch, post } from '$lib/api';
  import { FormState } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import Field from './Field.svelte';
  import FormDialog from './FormDialog.svelte';
  import Input from './Input.svelte';

  type Props = {
    open?: boolean;
    /** API kind, e.g. `nodes`. */
    kind: string;
    /** Display noun, e.g. `节点`. */
    label: string;
    /** Rename this group; omit to create one. */
    group?: { id: number; name: string } | null;
    onsaved: () => void;
  };
  let { open = $bindable(false), kind, label, group = null, onsaved }: Props = $props();
  const form = new FormState(['name']);
  let name = $state('');
  let initial = $state('');

  $effect(() => {
    if (open) {
      initial = name = group?.name ?? '';
      form.clear();
    }
  });

  async function submit() {
    const ok = await form.run(() => (group ? patch(`groups/${group.id}`, { name }) : post('groups', { kind, name })));
    if (!ok) return;
    open = false;
    toast.ok('已保存');
    onsaved();
  }
</script>

<FormDialog bind:open title={group ? '分组改名' : `新建${label}分组`} size="sm" {form} dirty={name !== initial} onsubmit={submit}>
  <Field label="分组名称" error={form.errors.name}>
    {#snippet children({ id, invalid })}
      <Input {id} {invalid} bind:value={name} maxlength={64} required autofocus />
    {/snippet}
  </Field>
</FormDialog>
