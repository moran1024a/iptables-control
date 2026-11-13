<script lang="ts">
  import { untrack } from 'svelte';
  import { patch, post } from '$lib/api';
  import { FieldError, FormState, snapshot } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Group, Subscription, SubscriptionCandidate, SubscriptionSave } from '$lib/types';
  import Field from '$ui/Field.svelte';
  import FormDialog from '$ui/FormDialog.svelte';
  import Input from '$ui/Input.svelte';
  import Switch from '$ui/Switch.svelte';
  import { configKey } from '../configs/model';
  import ConfigPicker from './ConfigPicker.svelte';
  import { memberKeys, splitKeys } from './model';

  type Props = {
    open?: boolean;
    /** Edit this subscription; null creates one. */
    subscription: Subscription | null;
    catalog: SubscriptionCandidate[];
    groups: (Group & { source: 'topology' | 'external' })[];
    onsaved: () => void;
  };
  let { open = $bindable(false), subscription, catalog, groups, onsaved }: Props = $props();
  const uid = $props.id();

  const form = new FormState(['name']);
  let name = $state('');
  let enabled = $state(true);
  let selected = $state<string[]>([]);
  let locked = $state<string[]>([]);
  let initial = $state('');
  const current = () => snapshot({ name, enabled, selected });

  $effect(() => {
    if (open)
      untrack(() => {
        name = subscription?.name ?? '';
        enabled = subscription?.enabled ?? true;
        const members = new Set(subscription ? memberKeys(subscription) : []);
        // Catalog order, and only what still exists.
        locked = selected = catalog.map(configKey).filter((key) => members.has(key));
        initial = current();
        form.clear();
      });
  });

  async function submit() {
    const id = subscription?.id;
    const ok = await form.run(() => {
      if (!name.trim()) throw new FieldError('名称不能为空', 'name');
      const body: SubscriptionSave = { name: name.trim(), enabled, ...splitKeys(selected, catalog) };
      return id === undefined ? post('subscriptions', body) : patch(`subscriptions/${id}`, body);
    });
    if (!ok) return;
    open = false;
    toast.ok('订阅已保存');
    onsaved();
  }
</script>

<FormDialog bind:open title={subscription ? '编辑订阅' : '新增订阅'} size="lg" {form} dirty={current() !== initial} onsubmit={submit}>
  <div class="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-4">
    <Field label="名称" error={form.errors.name}>
      {#snippet children({ id, invalid })}
        <Input {id} {invalid} bind:value={name} maxlength={64} required autofocus={!subscription} placeholder="例如：手机客户端" />
      {/snippet}
    </Field>
    <div class="flex flex-col gap-1">
      <label for="{uid}-enabled" class="text-[0.8125rem] font-medium">启用</label>
      <div class="flex h-8 items-center gap-2">
        <Switch id="{uid}-enabled" bind:checked={enabled} />
        <span class="text-xs text-fg-muted">{enabled ? '链接可访问' : '链接返回 404'}</span>
      </div>
    </div>
  </div>
  <div class="flex flex-col gap-1.5">
    <p class="text-[0.8125rem] font-medium">包含的配置</p>
    <ConfigPicker {catalog} {groups} bind:selected {locked} />
    {#if catalog.length && !selected.length}
      <p class="text-xs text-warn-text">还没有选择配置：可以保存，但客户端拉取到的订阅是空的。</p>
    {/if}
  </div>
</FormDialog>
