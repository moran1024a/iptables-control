<script lang="ts">
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import { untrack } from 'svelte';
  import { api, ApiError, errorMessage, patch, post } from '$lib/api';
  import { FormState, snapshot } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { ExternalConfig, ExternalProtocol, ExternalSave, Group } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import Field from '$ui/Field.svelte';
  import FormDialog from '$ui/FormDialog.svelte';
  import Input from '$ui/Input.svelte';
  import Select from '$ui/Select.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Textarea from '$ui/Textarea.svelte';
  import { protocolName } from './model';

  type Parsed = { name: string; protocol: ExternalProtocol; host: string; port: number };
  type Preview =
    | { state: 'idle' | 'loading' }
    | { state: 'ok'; data: Parsed }
    /** `invalid`: the server rejected the link itself, as opposed to the request failing. */
    | { state: 'error'; message: string; invalid: boolean };

  type Props = {
    open?: boolean;
    /** Edit this configuration (as last loaded); null imports a new one. */
    config: ExternalConfig | null;
    /** External configuration groups. */
    groups: Group[];
    /** All external configurations, to point out duplicates. */
    existing: ExternalConfig[];
    onsaved: () => void;
    onrefresh: () => Promise<void>;
  };
  let { open = $bindable(false), config, groups, existing, onsaved, onrefresh }: Props = $props();

  const form = new FormState(['uri', 'name', 'group_id']);
  let uri = $state('');
  let name = $state('');
  let groupId = $state('');
  /** The user typed a name, so the parsed link name no longer replaces it. */
  let manual = $state(false);
  let initial = $state('');
  let preview = $state<Preview>({ state: 'idle' });
  /** The text the server last refused; its error disappears once the text changes. */
  let refused = $state('');
  let reloading = $state(false);
  const current = () => snapshot({ uri, name, groupId });

  function load() {
    uri = config?.uri ?? '';
    name = config?.name ?? '';
    groupId = String(config?.group_id ?? groups.find((group) => group.builtin)?.id ?? groups[0]?.id ?? '');
    manual = !!config;
    refused = '';
    preview = { state: 'idle' };
    initial = current();
    form.clear();
  }

  $effect(() => {
    if (open) untrack(load);
  });

  // Parse the link shortly after typing stops; an edit starts with the stored link already parsed.
  $effect(() => {
    if (!open) return;
    const text = uri.trim();
    if (!text) {
      preview = { state: 'idle' };
      return;
    }
    preview = { state: 'loading' };
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const data = await api<Parsed>('POST', 'external-config-preview', { uri: text }, { signal: controller.signal });
        if (controller.signal.aborted) return;
        preview = { state: 'ok', data };
        if (!manual) name = [...data.name].slice(0, 64).join('');
      } catch (err) {
        if (controller.signal.aborted) return;
        preview = { state: 'error', message: errorMessage(err), invalid: err instanceof ApiError && err.status === 400 };
      }
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  });

  const warnings = $derived.by(() => {
    if (preview.state !== 'ok') return [];
    const { data } = preview;
    const list: string[] = [];
    const twin = existing.find(
      (other) =>
        other.id !== config?.id &&
        other.connection.protocol === data.protocol &&
        other.connection.host.toLowerCase() === data.host.toLowerCase() &&
        other.connection.port === data.port,
    );
    if (twin) list.push(`已有相同协议和地址的配置 ${twin.code}「${twin.name}」，保存后会并存。`);
    if (data.protocol === 'http') list.push('v2rayN 不支持 HTTP/HTTPS 分享链接，这条配置只会出现在 Mihomo 订阅里。');
    if ([...data.name].length > 64) list.push('链接里的名称超过 64 个字符，自动填入时已截断。');
    return list;
  });
  const uriError = $derived(
    (form.errors.uri && refused === uri ? form.errors.uri : '') || (preview.state === 'error' && preview.invalid ? preview.message : ''),
  );

  async function submit() {
    const sent = uri;
    const body: ExternalSave = { name: name.trim(), uri: uri.trim(), revision: config?.revision ?? 0 };
    if (groups.length > 1 && groupId) body.group_id = Number(groupId);
    const id = config?.id;
    const ok = await form.run(() => (id === undefined ? post('external-configs', body) : patch(`external-configs/${id}`, body)));
    if (!ok) {
      refused = sent;
      return;
    }
    open = false;
    toast.ok('配置已保存');
    onsaved();
  }

  /** After an edit conflict: start over from what is stored now. */
  async function reload() {
    reloading = true;
    try {
      await onrefresh();
      if (!config) {
        open = false;
        toast.info('该配置已被删除');
        return;
      }
      load();
    } finally {
      reloading = false;
    }
  }
</script>

<FormDialog
  bind:open
  title={config ? `编辑配置 ${config.code}` : '导入配置'}
  description={config ? undefined : '把已有的节点分享链接收进面板，之后可以放进订阅统一下发。'}
  {form}
  dirty={current() !== initial}
  submitDisabled={!uri.trim() || (preview.state === 'error' && preview.invalid)}
  onsubmit={submit}
>
  <Field label="节点链接" error={uriError}>
    {#snippet children({ id, invalid })}
      <Textarea {id} {invalid} bind:value={uri} mono rows={4} maxlength={8192} required autofocus={!config} placeholder="粘贴一条节点分享链接，例如 trojan://… 或 ss://…" spellcheck={false} />
    {/snippet}
  </Field>

  <div class="-mt-2 rounded-md border bg-surface-2 px-3 py-2 text-[0.8125rem] {preview.state === 'error' && preview.invalid ? 'hidden' : ''}" aria-live="polite">
    {#if preview.state === 'ok'}
      <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
        <dt class="text-fg-muted">协议</dt>
        <dd class="font-medium">{protocolName(preview.data.protocol)}</dd>
        <dt class="text-fg-muted">服务器</dt>
        <dd class="min-w-0"><code class="break-all">{preview.data.host}</code></dd>
        <dt class="text-fg-muted">端口</dt>
        <dd><code>{preview.data.port}</code></dd>
        <dt class="text-fg-muted">链接名称</dt>
        <dd class="min-w-0 break-words">{preview.data.name || '（链接里没有名称）'}</dd>
      </dl>
      {#each warnings as warning (warning)}
        <p class="mt-2 flex items-start gap-1.5 text-xs text-warn-text"><TriangleAlert class="mt-px size-3.5 shrink-0" />{warning}</p>
      {/each}
    {:else if preview.state === 'loading'}
      <p class="flex items-center gap-2 text-fg-muted"><Spinner />正在解析链接…</p>
    {:else if preview.state === 'error'}
      <p class="text-warn-text">预览失败：{preview.message}。仍可直接保存，服务端会再次校验。</p>
    {:else}
      <p class="text-xs leading-relaxed text-fg-muted">
        粘贴后在这里预览解析结果。支持 SS · VMess · VLESS · Trojan · Hysteria2 · TUIC · AnyTLS · SOCKS5 · HTTP/HTTPS，每次一条，不支持订阅地址。
      </p>
    {/if}
  </div>

  <Field
    label="名称"
    optional
    error={form.errors.name}
    hint={config ? '留空则保持原名称。名称会写进复制出的分享链接和订阅。' : '留空则使用链接里的名称。名称会写进复制出的分享链接和订阅。'}
  >
    {#snippet children({ id, invalid })}
      <Input {id} {invalid} bind:value={name} maxlength={64} placeholder={config ? config.name : '自动识别链接名称'} oninput={() => (manual = name !== '')} />
    {/snippet}
  </Field>
  {#if groups.length > 1}
    <Field label="分组" error={form.errors.group_id}>
      {#snippet children({ id, invalid })}
        <Select {id} {invalid} bind:value={groupId} options={groups.map((group) => ({ value: String(group.id), label: group.name }))} />
      {/snippet}
    </Field>
  {/if}
  {#snippet erroraction()}
    {#if form.status === 409}
      <Button size="sm" loading={reloading} onclick={reload}>放弃修改并载入最新</Button>
    {/if}
  {/snippet}
</FormDialog>
