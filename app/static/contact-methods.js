'use strict';
// Existing password-only accounts need an in-app route to attach a recoverable login method.
(() => {
  const el = id => document.getElementById(id);
  const banner = el('contact-method-banner');
  const dialog = el('contact-email-dialog');
  const status = el('contact-email-status');
  let verifying = false;

  async function verifyLink() {
    if (verifying || el('workspace').hidden) return;
    const match = /^#verify-email=([A-Za-z0-9_-]{20,})$/.exec(location.hash);
    if (!match) return;
    verifying = true;
    try {
      await api('/api/v1/account/email/verify', {method:'POST', body:JSON.stringify({token:match[1]})});
      history.replaceState(history.state, '', location.pathname + location.search);
      if (dialog.open) dialog.close();
      message('Почта подтверждена и подключена к аккаунту.');
    } catch (error) {
      message(error.message);
    } finally { verifying = false; }
  }

  async function refresh() {
    if (el('workspace').hidden) { banner.hidden = true; return; }
    await verifyLink();
    try {
      const methods = await api('/api/v1/account/login-methods');
      banner.hidden = methods.email_verified || methods.telegram_linked;
    } catch { banner.hidden = true; }
  }

  el('contact-email').onclick = () => {
    status.textContent = '';
    dialog.showModal();
    el('contact-email-address').focus();
  };
  el('contact-telegram').onclick = () => el('open-telegram').click();
  el('contact-email-form').onsubmit = async event => {
    event.preventDefault();
    const button = el('contact-email-send');
    button.disabled = true; status.textContent = '';
    try {
      await api('/api/v1/account/email', {method:'POST', body:JSON.stringify({email:el('contact-email-address').value})});
      status.textContent = 'Письмо отправлено. Откройте ссылку в этом браузере, пока вы вошли в аккаунт.';
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  window.addEventListener('hashchange', () => { refresh(); });
  window.addEventListener('beresta:telegram-linked', () => { refresh(); });
  new MutationObserver(() => { if (el('workspace').hidden) banner.hidden = true; })
    .observe(el('workspace'), {attributes:true, attributeFilter:['hidden']});
  window.BerestaContact = {refresh};
})();
