'use strict';
// Password change in the account. An account without a password only gets a short explanation.
// Loaded after app.js. It uses api() and message() from there.
(() => {
  const el = id => document.getElementById(id);
  const opener = el('open-password');
  if (!opener) return;
  const fields = ['password-old', 'password-new', 'password-new2'];
  const length = value => [...value].length;

  function openDialog(id) {
    const dialog = el(id);
    if (!dialog.open) dialog.showModal();
    return dialog;
  }

  function showError(field, text) {
    const note = el(field + '-error');
    note.textContent = text; note.hidden = !text;
    el(field).toggleAttribute('aria-invalid', Boolean(text));
  }

  function clearErrors() {
    for (const field of ['password-old', 'password-new2']) showError(field, '');
    el('password-status').textContent = '';
  }

  function reset() {
    for (const id of fields) el(id).value = '';
    clearErrors();
    el('password-submit').disabled = false;
  }

  opener.onclick = async () => {
    let account;
    try { account = await api('/api/v1/auth/me'); }
    catch (e) { message(e.message); return; }
    // An older answer without the flag means an account with a password.
    if (account.has_password === false) { openDialog('nopassword-dialog'); return; }
    reset();
    openDialog('password-dialog');
    el('password-old').focus();
  };

  el('password-dialog').addEventListener('close', reset);

  el('password-form').onsubmit = async event => {
    event.preventDefault();
    clearErrors();
    const oldPassword = el('password-old').value, next = el('password-new').value, again = el('password-new2').value;
    if (!oldPassword) { showError('password-old', 'Введите текущий пароль.'); el('password-old').focus(); return; }
    if (length(next) < 10 || length(next) > 128) {
      el('password-status').textContent = 'Новый пароль должен содержать от 10 до 128 символов.'; el('password-new').focus(); return;
    }
    if (next !== again) { showError('password-new2', 'Пароли не совпадают'); el('password-new2').focus(); return; }
    if (next === oldPassword) {
      el('password-status').textContent = 'Новый пароль должен отличаться от текущего.'; el('password-new').focus(); return;
    }
    const button = el('password-submit');
    button.disabled = true;
    try {
      await api('/api/v1/account/password', {method: 'POST', body: JSON.stringify({old_password: oldPassword, new_password: next})});
      for (const id of fields) el(id).value = '';
      el('password-status').textContent = 'Пароль изменён. Остальные входы в аккаунт завершены.';
    } catch (e) {
      if (e.status === 409 && e.data && e.data.code === 'no_password') {
        el('password-dialog').close(); openDialog('nopassword-dialog'); return;
      }
      if (e.status === 403) { showError('password-old', 'Неверный текущий пароль'); el('password-old').focus(); }
      else el('password-status').textContent = e.message;
    } finally { button.disabled = false; }
  };
})();
