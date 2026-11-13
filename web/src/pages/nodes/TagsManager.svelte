<script lang="ts">
  import Pencil from '@lucide/svelte/icons/pencil';
  import Plus from '@lucide/svelte/icons/plus';
  import TagsIcon from '@lucide/svelte/icons/tags';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { del, errorMessage, get, patch, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { FormState } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { NodesList, Tag } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Dialog from '$ui/Dialog.svelte';
  import EmptyState from '$ui/EmptyState.svelte';
  import Input from '$ui/Input.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import TagBadge from '$ui/TagBadge.svelte';

  /** `onchange` runs after any tag was created, edited or deleted. */
  let { open = $bindable(false), onchange }: { open?: boolean; onchange: () => void } = $props();

  const DEFAULT_COLOR = '#3b82f6';
  // Tag colours are user data; these are only shortcuts for the picker.
  const PRESETS = ['#ef4444', '#f59e0b', '#84cc16', '#10b981', '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899', '#64748b'];

  let tags = $state.raw<Tag[]>([]);
  let usage = $state.raw<Record<number, number>>({});
  let loading = $state(false);
  let loadError = $state('');
  /** Tag id being edited, 0 for a new tag, null when no editor is open. */
  let editing = $state<number | null>(null);
  let name = $state('');
  let color = $state(DEFAULT_COLOR);
  let start = '';
  let deleting = $state(0);
  const form = new FormState(['name', 'color']);
  const dirty = $derived(editing !== null && `${name}|${color}` !== start);

  async function load() {
    loadError = '';
    try {
      // The node list carries the catalogue and tells how many nodes use each tag.
      const list = await get<NodesList>('nodes');
      tags = list.tags;
      const counts: Record<number, number> = {};
      for (const node of list.nodes) for (const tag of node.tags) counts[tag.id] = (counts[tag.id] ?? 0) + 1;
      usage = counts;
    } catch (err) {
      loadError = errorMessage(err);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    if (!open) return;
    editing = null;
    form.clear();
    loading = true;
    void load();
  });

  function begin(tag: Tag | null) {
    editing = tag?.id ?? 0;
    name = tag?.name ?? '';
    color = tag && /^#[0-9a-f]{6}$/i.test(tag.color) ? tag.color.toLowerCase() : DEFAULT_COLOR;
    start = `${name}|${color}`;
    form.clear();
  }
  async function leave(): Promise<boolean> {
    if (form.busy) return false;
    if (dirty && !(await confirm({ title: '放弃未保存的修改？', confirmLabel: '放弃修改', cancelLabel: '继续编辑', danger: true }))) return false;
    editing = null;
    return true;
  }
  async function switchTo(tag: Tag | null) {
    if (editing === null || (await leave())) begin(tag);
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    const id = editing;
    if (id === null) return;
    if (!name.trim()) {
      form.clear();
      form.fail('标签名称不能为空', 'name');
      return;
    }
    const ok = await form.run(() => (id ? patch(`tags/${id}`, { name, color }) : post('tags', { name, color })));
    if (!ok) return;
    editing = null;
    toast.ok('标签已保存');
    await load();
    onchange();
  }

  async function remove(tag: Tag) {
    if (deleting || form.busy) return;
    const used = usage[tag.id] ?? 0;
    const ok = await confirm({
      title: '删除标签',
      body: used ? `删除「${tag.name}」并解除 ${used} 个节点上的关联？` : `删除「${tag.name}」？没有节点使用此标签。`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    deleting = tag.id;
    try {
      await del(`tags/${tag.id}`);
      if (editing === tag.id) editing = null;
      toast.ok('标签已删除');
      await load();
      onchange();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      deleting = 0;
    }
  }
</script>

{#snippet editor()}
  <form onsubmit={save} class="flex flex-col gap-2.5 bg-surface-2/60 px-5 py-3" inert={form.busy} data-testid="tag-editor">
    <div class="flex items-center gap-2">
      <label class="sr-only" for="tag-name">名称</label>
      <Input id="tag-name" bind:value={name} invalid={!!form.errors.name} maxlength={64} placeholder="标签名称" oninput={() => form.clear()} autofocus autocomplete="off" class="flex-1" />
      <TagBadge tag={{ name: name.trim() || '预览', color }} class="max-sm:hidden" />
    </div>
    <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="颜色">
      {#each PRESETS as preset (preset)}
        <button
          type="button"
          aria-label="颜色 {preset}"
          aria-pressed={color === preset}
          onclick={() => (color = preset)}
          style:background-color={preset}
          class="size-6 rounded-full border border-black/10 outline-offset-2 {color === preset ? 'outline-2 outline-(--fg)' : ''}"
        ></button>
      {/each}
      <label class="ml-1 inline-flex h-6 cursor-pointer items-center gap-1.5 rounded-md border bg-surface pr-2 pl-1 text-xs text-fg-muted">
        <input type="color" bind:value={color} class="size-4 cursor-pointer rounded-sm border-0 bg-transparent p-0" />自定义
      </label>
    </div>
    {#if form.errors.name || form.errors.color || form.error}
      <p class="text-xs text-danger-text" role="alert">{form.errors.name || form.errors.color || form.error}</p>
    {/if}
    <div class="flex justify-end gap-2">
      <Button size="sm" onclick={leave} disabled={form.busy}>取消</Button>
      <Button size="sm" type="submit" variant="primary" loading={form.busy}>{editing ? '保存' : '创建'}</Button>
    </div>
  </form>
{/snippet}

<Dialog bind:open title="节点标签" description="标签用于在列表和链路中标记、筛选节点；在节点的编辑表单中分配。" beforeclose={async () => editing === null || (await leave())}>
  <div class="-mx-5 -my-4">
    {#if loading && !tags.length}
      <div class="flex justify-center py-12 text-fg-subtle"><Spinner class="size-5" /></div>
    {:else}
      {#if loadError}<div class="px-5 pt-4"><Alert tone="danger">{loadError}</Alert></div>{/if}
      {#if !tags.length && editing === null && !loadError}
        <EmptyState icon={TagsIcon} title="暂无标签" text="例如按地区、线路或用途给节点分类。" class="py-10">
          <Button variant="primary" onclick={() => begin(null)}><Plus />新建标签</Button>
        </EmptyState>
      {/if}
      <ul class="divide-y">
        {#each tags as tag (tag.id)}
          <li>
            {#if editing === tag.id}
              {@render editor()}
            {:else}
              <div class="flex min-h-11 items-center gap-2 px-5 py-1.5">
                <TagBadge {tag} class="max-w-48" />
                <span class="ml-auto shrink-0 text-xs text-fg-subtle">{usage[tag.id] ? `${usage[tag.id]} 个节点` : '未使用'}</span>
                <Button size="sm" variant="ghost" icon aria-label="编辑 {tag.name}" title="编辑" onclick={() => switchTo(tag)}><Pencil /></Button>
                <Button size="sm" variant="danger-ghost" icon aria-label="删除 {tag.name}" title="删除" loading={deleting === tag.id} disabled={!!deleting} onclick={() => remove(tag)}>
                  {#if deleting !== tag.id}<Trash2 />{/if}
                </Button>
              </div>
            {/if}
          </li>
        {/each}
        {#if editing === 0}<li>{@render editor()}</li>{/if}
      </ul>
    {/if}
  </div>
  {#snippet footer()}
    {#if tags.length && editing !== 0}
      <Button class="mr-auto" onclick={() => switchTo(null)}><Plus />新建标签</Button>
    {/if}
    <Button onclick={async () => (editing === null || (await leave())) && (open = false)}>关闭</Button>
  {/snippet}
</Dialog>
