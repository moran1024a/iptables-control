<script lang="ts">
  import { FormState } from '$lib/form.svelte';
  import { session } from '$lib/session.svelte';
  import { toast } from '$lib/toast.svelte';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import Field from '$ui/Field.svelte';
  import Input from '$ui/Input.svelte';
  import { checkAccount, type AccountDraft } from './settings';

  /** `dirty` tells the page that leaving would lose typed input. */
  let { dirty = $bindable(false) }: { dirty?: boolean } = $props();

  const form = new FormState(['current_password', 'username', 'password', 'repeat']);
  const blank = (): AccountDraft => ({ current: '', username: session.username, password: '', repeat: '' });
  let draft = $state<AccountDraft>(blank());
  const changed = $derived(draft.username.trim() !== session.username || !!draft.password || !!draft.repeat);
  $effect(() => {
    dirty = changed || !!draft.current;
  });

  function reset() {
    draft = blank();
    form.clear();
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!(await form.run(() => session.changeAccount(checkAccount(draft))))) return;
    reset();
    toast.ok('账号已更新');
  }
</script>

<Card title="账号" hint="登录面板使用的管理员账号">
  <form onsubmit={submit} class="flex flex-col gap-3.5" inert={form.busy} aria-busy={form.busy} novalidate>
    <Field label="用户名" hint="1-32 位字母、数字或 _ . @ -" error={form.errors.username}>
      {#snippet children({ id, invalid })}
        <Input {id} {invalid} bind:value={draft.username} autocomplete="username" maxlength={32} spellcheck={false} autocapitalize="off" />
      {/snippet}
    </Field>
    <div class="grid gap-3.5 sm:grid-cols-2">
      <Field label="新密码" hint="8-128 位，留空则不修改" error={form.errors.password}>
        {#snippet children({ id, invalid })}
          <Input {id} {invalid} type="password" bind:value={draft.password} autocomplete="new-password" maxlength={128} />
        {/snippet}
      </Field>
      <Field label="确认新密码" error={form.errors.repeat}>
        {#snippet children({ id, invalid })}
          <Input {id} {invalid} type="password" bind:value={draft.repeat} autocomplete="new-password" maxlength={128} />
        {/snippet}
      </Field>
    </div>
    <Field label="当前密码" hint="修改用户名或密码都需要先验证当前密码。" error={form.errors.current_password}>
      {#snippet children({ id, invalid })}
        <Input {id} {invalid} type="password" bind:value={draft.current} autocomplete="current-password" />
      {/snippet}
    </Field>
    {#if form.error}<Alert tone="danger">{form.error}</Alert>{/if}
    <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-t pt-3.5">
      <p class="text-xs text-fg-muted">保存后，其他浏览器和设备上的登录会被退出，当前登录保持不变。</p>
      <div class="ml-auto flex gap-2">
        {#if dirty}<Button variant="ghost" onclick={reset} disabled={form.busy}>还原</Button>{/if}
        <Button type="submit" variant="primary" loading={form.busy} disabled={!changed}>{form.busy ? '处理中…' : '保存账号'}</Button>
      </div>
    </div>
  </form>
</Card>
