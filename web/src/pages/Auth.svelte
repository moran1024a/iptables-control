<script lang="ts">
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import { ApiError, errorMessage } from '$lib/api';
  import { session } from '$lib/session.svelte';
  import { toast } from '$lib/toast.svelte';
  import Button from '$ui/Button.svelte';
  import Field from '$ui/Field.svelte';
  import Input from '$ui/Input.svelte';
  import Logo from '../layout/Logo.svelte';

  // Kept across the anonymous → must-change transition so the default password is typed once.
  let username = $state('');
  let password = $state('');
  let newUsername = $state('');
  let newPassword = $state('');
  let repeat = $state('');
  let carried = $state(false);
  let busy = $state(false);
  let error = $state('');
  let field = $state<string | undefined>();
  const changing = $derived(session.status === 'must-change');
  const insecure = location.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    error = '';
    field = undefined;
    if (changing && newPassword !== repeat) {
      error = '两次输入的新密码不一致';
      field = 'repeat';
      return;
    }
    busy = true;
    try {
      if (changing) {
        await session.changeAccount({ current_password: password, username: newUsername.trim(), password: newPassword });
        toast.ok('账号已更新');
      } else {
        await session.login(username, password);
        carried = true;
      }
    } catch (err) {
      error = errorMessage(err);
      field = err instanceof ApiError ? err.field : undefined;
    } finally {
      busy = false;
    }
  }
</script>

<div class="flex min-h-dvh flex-col items-center justify-center px-4 py-10">
  <form onsubmit={submit} class="w-full max-w-sm rounded-xl border bg-surface p-7 shadow-md">
    <div class="mb-6 flex items-center gap-2.5">
      <Logo class="size-8" />
      <div>
        <h1 class="text-base leading-tight font-semibold">ictl</h1>
        <p class="text-xs text-fg-muted">{changing ? '首次登录，请设置新的管理员账号' : '端口转发管理面板'}</p>
      </div>
    </div>
    {#if insecure}
      <div class="mb-4 flex items-start gap-2 rounded-lg bg-warn-soft px-3 py-2 text-xs leading-relaxed text-warn-text">
        <TriangleAlert class="mt-0.5 size-3.5 shrink-0" />
        当前为 HTTP 连接，登录密码会以明文传输，建议配置 HTTPS。
      </div>
    {/if}
    <div class="flex flex-col gap-3.5">
      {#if changing}
        <p class="text-[0.8667rem] leading-relaxed text-fg-muted">默认账号仅用于首次登录。设置新的用户名和密码后才能使用面板。</p>
        {#if !carried}
          <Field label="当前密码" error={field === 'current_password' ? error : null}>
            {#snippet children({ id, invalid })}
              <Input {id} {invalid} type="password" bind:value={password} autocomplete="current-password" required />
            {/snippet}
          </Field>
        {/if}
        <Field label="新用户名" hint="1-32 位字母、数字或 _ . @ -" error={field === 'username' ? error : null}>
          {#snippet children({ id, invalid })}
            <Input {id} {invalid} bind:value={newUsername} autocomplete="username" required maxlength={32} autofocus />
          {/snippet}
        </Field>
        <Field label="新密码" hint="8-128 位" error={field === 'password' ? error : null}>
          {#snippet children({ id, invalid })}
            <Input {id} {invalid} type="password" bind:value={newPassword} autocomplete="new-password" required minlength={8} maxlength={128} />
          {/snippet}
        </Field>
        <Field label="再次输入新密码" error={field === 'repeat' ? error : null}>
          {#snippet children({ id, invalid })}
            <Input {id} {invalid} type="password" bind:value={repeat} autocomplete="new-password" required />
          {/snippet}
        </Field>
      {:else}
        <Field label="用户名">
          {#snippet children({ id })}
            <Input {id} bind:value={username} autocomplete="username" required autofocus />
          {/snippet}
        </Field>
        <Field label="密码">
          {#snippet children({ id })}
            <Input {id} type="password" bind:value={password} autocomplete="current-password" required />
          {/snippet}
        </Field>
      {/if}
      {#if error && !['username', 'password', 'repeat', 'current_password'].includes(field ?? '')}
        <p class="text-[0.8667rem] text-danger-text" role="alert">{error}</p>
      {/if}
      <Button type="submit" variant="primary" loading={busy} class="mt-1 w-full">{changing ? '保存并进入面板' : '登录'}</Button>
      {#if changing}
        <button type="button" class="text-xs text-fg-muted hover:text-fg" onclick={() => session.logout()}>退出登录</button>
      {/if}
    </div>
  </form>
</div>
