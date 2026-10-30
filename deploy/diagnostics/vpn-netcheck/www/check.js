'use strict';
const report = document.getElementById('report');
const statusLine = document.getElementById('status');
let openedAt = '';

function updateReport() {
  const result = document.getElementById('main-result');
  const vpnState = document.getElementById('vpn-state');
  const vpnName = document.getElementById('vpn-name').value.trim();
  report.value = [
    'Дата отчёта UTC: ' + new Date().toISOString(),
    'Версия проверки: 3, ручное сравнение',
    'Тестовая страница: открылась',
    'Основной адрес: https://app.berestaapp.ru/',
    'Ссылка открыта для проверки UTC: ' + (openedAt || 'не зафиксировано'),
    'Результат обычного открытия: ' + result.options[result.selectedIndex].text,
    'VPN включён: ' + vpnState.options[vpnState.selectedIndex].text,
    'Название VPN: ' + (vpnName || 'не указано'),
    'Автоматические межсайтовые запросы не выполняются.',
  ].join('\n');
}

document.getElementById('open-main').addEventListener('click', () => {
  openedAt = new Date().toISOString();
  updateReport();
});
for (const name of ['main-result', 'vpn-state', 'vpn-name']) {
  document.getElementById(name).addEventListener('input', updateReport);
  document.getElementById(name).addEventListener('change', updateReport);
}
document.getElementById('copy').addEventListener('click', async () => {
  updateReport();
  try {
    await navigator.clipboard.writeText(report.value);
    statusLine.textContent = 'Результат скопирован.';
  } catch {
    report.focus();
    report.select();
    statusLine.textContent = 'Выделили результат. Скопируйте его обычным способом.';
  }
});
updateReport();
