'use strict';
const button = document.getElementById('check');
const statusLine = document.getElementById('status');
const report = document.getElementById('report');

button.addEventListener('click', async () => {
  button.disabled = true;
  statusLine.textContent = 'Проверяем соединение, до 10 секунд…';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  const started = performance.now();
  const date = new Date().toISOString();
  let result;
  let detail = '';
  try {
    // A public SVG avoids browser blocking of cross-origin opaque JSON (ORB).
    // Opaque mode confirms transport only. It cannot reveal the response status.
    await fetch('https://app.berestaapp.ru/static/favicon.svg', {
      mode: 'no-cors', credentials: 'omit', cache: 'no-store',
      referrerPolicy: 'no-referrer', redirect: 'error', signal: controller.signal,
    });
    result = 'Публичный ресурс основного сайта получен. Вход и работа приложения отдельно не проверялись.';
  } catch (error) {
    detail = error.name + ': ' + error.message;
    result = error.name === 'AbortError'
      ? 'За 10 секунд основной адрес не ответил.'
      : 'Браузер не смог получить ответ основного адреса. Это может быть DNS, TLS, сеть или блокировка браузером.';
  } finally {
    clearTimeout(timer);
    button.disabled = false;
  }
  const elapsed = Math.round(performance.now() - started);
  statusLine.textContent = result;
  report.value = [
    'Дата UTC: ' + date,
    'Тестовая страница: открылась',
    'Версия проверки: 2',
    'Основной адрес: https://app.berestaapp.ru/static/favicon.svg',
    result,
    ...(detail ? ['Подробность браузера: ' + detail] : []),
    'Время: ' + elapsed + ' мс',
    'VPN оставался включён: допишите да или нет',
    'Название VPN: допишите',
    'Ошибка при обычном открытии сайта: допишите, если есть',
  ].join('\n');
});

document.getElementById('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(report.value);
    statusLine.textContent = 'Результат скопирован.';
  } catch {
    report.focus();
    report.select();
    statusLine.textContent = 'Выделили результат. Скопируйте его обычным способом.';
  }
});
