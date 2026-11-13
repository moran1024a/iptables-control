<script lang="ts">
  import { untrack } from 'svelte';
  import { patch } from '$lib/api';
  import { FormState } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Settings } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import Field from '$ui/Field.svelte';
  import Input from '$ui/Input.svelte';
  import { checkOfflineSeconds, checkPublicUrl, duration, normaliseUrl, OFFLINE_DEFAULT, OFFLINE_MAX, OFFLINE_MIN } from './settings';

  type Saved = Pick<Settings, 'public_url' | 'failover_offline_seconds'>;
  type Props = { settings: Settings; dirty?: boolean; onsaved: () => void };
  let { settings, dirty = $bindable(false), onsaved }: Props = $props();

  const form = new FormState(['public_url', 'failover_offline_seconds']);
  // `saved` is what the server holds; the inputs start from it and return to it after every save.
  const first = untrack(() => settings);
  let saved = $state<Saved>({ public_url: first.public_url, failover_offline_seconds: first.failover_offline_seconds });
  let url = $state(first.public_url);
  let seconds = $state(String(first.failover_offline_seconds));
  const urlChanged = $derived(normaliseUrl(url) !== saved.public_url);
  const secondsChanged = $derived(seconds.trim() !== String(saved.failover_offline_seconds));
  const changed = $derived(urlChanged || secondsChanged);
  $effect(() => {
    dirty = changed;
  });

  /** Adopt server values, keeping whatever the user is in the middle of changing. */
  function adopt(next: Saved, force = false) {
    if (force || !urlChanged) url = next.public_url;
    if (force || !secondsChanged) seconds = String(next.failover_offline_seconds);
    saved = { public_url: next.public_url, failover_offline_seconds: next.failover_offline_seconds };
  }
  $effect(() => {
    const next = settings;
    untrack(() => adopt(next));
  });

  const origin = location.origin;
  const parsedSeconds = $derived(/^[0-9]{1,5}$/.test(seconds.trim()) ? Number(seconds.trim()) : null);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const ok = await form.run(async () => {
      const body: Partial<Saved> = {};
      if (urlChanged) body.public_url = checkPublicUrl(url);
      if (secondsChanged) body.failover_offline_seconds = checkOfflineSeconds(seconds);
      adopt(await patch<Saved>('settings', body), true);
    });
    if (!ok) return;
    toast.ok('已保存');
    onsaved();
  }
</script>

<Card title="服务端" hint="对所有节点和链路生效">
  <form onsubmit={submit} class="flex flex-col gap-3.5" inert={form.busy} aria-busy={form.busy} novalidate>
    <Field
      label="对外地址"
      optional
      error={form.errors.public_url}
      hint="节点用它连接面板，一键接入命令和同步地址都由它生成。格式为 http(s)://域名或IP[:端口]，不带路径。留空时使用当前访问面板的地址（{origin}）。"
    >
      {#snippet children({ id, invalid })}
        <Input {id} {invalid} mono bind:value={url} placeholder="https://example.com" maxlength={300} autocomplete="off" spellcheck={false} autocapitalize="off" inputmode="url" />
      {/snippet}
    </Field>
    <Field
      label="失联等待时间"
      error={form.errors.failover_offline_seconds}
      hint="节点失联超过这个时间后，开启了自动灾备的链路会切换到备用路径。范围 {OFFLINE_MIN}～{OFFLINE_MAX} 秒，默认 {OFFLINE_DEFAULT} 秒。"
    >
      {#snippet children({ id, invalid })}
        <div class="flex items-center gap-2">
          <div class="w-24 shrink-0">
            <Input {id} {invalid} bind:value={seconds} class="tabular" inputmode="numeric" maxlength={5} autocomplete="off" aria-describedby="{id}-unit" />
          </div>
          <span id="{id}-unit" class="text-[0.8667rem] whitespace-nowrap text-fg-muted">
            秒{#if parsedSeconds !== null && parsedSeconds >= 60 && parsedSeconds <= OFFLINE_MAX}<span class="text-fg-subtle">（{duration(parsedSeconds)}）</span>{/if}
          </span>
        </div>
      {/snippet}
    </Field>
    {#if form.error}<Alert tone="danger">{form.error}</Alert>{/if}
    <div class="flex justify-end gap-2 border-t pt-3.5">
      {#if changed}
        <Button
          variant="ghost"
          disabled={form.busy}
          onclick={() => {
            adopt(saved, true);
            form.clear();
          }}
        >
          还原
        </Button>
      {/if}
      <Button type="submit" variant="primary" loading={form.busy} disabled={!changed}>{form.busy ? '处理中…' : '保存设置'}</Button>
    </div>
  </form>
</Card>
