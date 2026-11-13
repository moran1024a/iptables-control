<script lang="ts">
  import { api, ApiError, errorMessage } from '$lib/api';
  import type { NodeRule, QueryOk } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import CopyButton from '$ui/CopyButton.svelte';
  import Dialog from '$ui/Dialog.svelte';
  import Field from '$ui/Field.svelte';
  import Select from '$ui/Select.svelte';
  import { formatQueryResult, needsRule, QUERY_KINDS, type QueryKind } from './node-logic';

  type Props = { open?: boolean; nodeId: number; nodeName: string; rules: NodeRule[] };
  let { open = $bindable(false), nodeId, nodeName, rules }: Props = $props();

  let kind = $state<QueryKind>('inventory');
  let ruleId = $state(0);
  let busy = $state(false);
  let error = $state('');
  let warning = $state('');
  let result = $state('');
  let resultTitle = $state('');
  let raw = $state(false);
  let rawText = $state('');
  let controller: AbortController | undefined;

  const ruleNeeded = $derived(needsRule(kind));
  const ruleOptions = $derived(rules.map((rule) => ({ value: rule.id, label: `${rule.configuration_name} · ${rule.port}` })));
  const blocked = $derived(ruleNeeded && !rules.length);

  $effect(() => {
    if (!open) return;
    error = warning = result = rawText = '';
    raw = false;
    return () => controller?.abort();
  });
  $effect(() => {
    if (!rules.some((rule) => rule.id === ruleId)) ruleId = rules[0]?.id ?? 0;
  });

  async function run(event: SubmitEvent) {
    event.preventDefault();
    if (busy || blocked) return;
    busy = true;
    error = warning = result = rawText = '';
    const current = (controller = new AbortController());
    const label = QUERY_KINDS.find((item) => item.value === kind)?.label ?? kind;
    try {
      // The server waits up to 32 s for the agent, so the client allows 45 s.
      const body = ruleNeeded ? { kind, rule_id: ruleId } : { kind };
      const reply = await api<QueryOk>('POST', `nodes/${nodeId}/query`, body, { timeout: 45000, signal: current.signal });
      if (reply.error) {
        error = reply.error;
        return;
      }
      const data = (reply.data ?? reply) as Record<string, unknown>;
      if (typeof data.error === 'string' && data.error) warning = data.error;
      result = formatQueryResult(data);
      rawText = JSON.stringify(data, null, 2);
      resultTitle = label;
    } catch (err) {
      if (current.signal.aborted && !(err instanceof ApiError)) error = '已取消本次检查';
      else if (err instanceof ApiError && err.status === 0 && err.message.startsWith('请求超时')) error = '检查超时，请重试';
      else error = errorMessage(err);
    } finally {
      if (controller === current) busy = false;
    }
  }
</script>

<Dialog bind:open title="节点诊断" description="向 {nodeName} 上的主动 Agent 发起一次只读检查" size="lg" beforeclose={() => !busy}>
  <form id="diagnostics-form" onsubmit={run} class="flex flex-col gap-4">
    <div class="grid gap-3 sm:grid-cols-2">
      <Field label="检查项目">
        {#snippet children({ id })}
          <Select {id} bind:value={kind} disabled={busy} options={QUERY_KINDS.map((item) => ({ value: item.value, label: item.label }))} />
        {/snippet}
      </Field>
      {#if ruleNeeded}
        <Field label="转发规则" error={blocked ? '此节点没有转发规则，无法执行这项检查' : ''}>
          {#snippet children({ id, invalid })}
            <Select {id} {invalid} bind:value={ruleId} disabled={busy || blocked} options={ruleOptions} />
          {/snippet}
        </Field>
      {/if}
    </div>
    {#if busy}
      <p class="text-[0.8667rem] text-fg-muted" role="status">正在等待节点返回结果，最长约 {kind === 'diagnose' ? 32 : 12} 秒…</p>
    {/if}
    {#if error}<Alert tone="danger">{error}</Alert>{/if}
    {#if warning}<Alert tone="warn" title="节点返回错误">{warning}</Alert>{/if}
    {#if result}
      <div class="overflow-hidden rounded-md border">
        <div class="flex items-center justify-between gap-2 border-b bg-surface-2 py-1 pr-1 pl-3">
          <span class="truncate text-xs font-medium text-fg-muted">{resultTitle}</span>
          <div class="flex items-center gap-1">
            <Button size="xs" variant="ghost" aria-pressed={raw} onclick={() => (raw = !raw)}>{raw ? '整理视图' : '原始 JSON'}</Button>
            <CopyButton text={() => (raw ? rawText : result)} label="复制" size="xs" />
          </div>
        </div>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex: scrollable region must be keyboard reachable -->
        <pre tabindex="0" class="max-h-[50vh] overflow-auto p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap" data-testid="diagnostic-result">{raw ? rawText : result}</pre>
      </div>
    {/if}
  </form>
  {#snippet footer()}
    {#if busy}
      <Button onclick={() => controller?.abort()}>取消检查</Button>
    {:else}
      <Button onclick={() => (open = false)}>关闭</Button>
    {/if}
    <Button type="submit" form="diagnostics-form" variant="primary" loading={busy} disabled={blocked}>{busy ? '检查中…' : '检查'}</Button>
  {/snippet}
</Dialog>
