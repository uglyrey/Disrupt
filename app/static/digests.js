'use strict';
(() => {
  const get = id => document.getElementById(id);
  let generation = 0, known = false;
  const zones = {'Europe/Moscow':'Москва','Europe/Kaliningrad':'Калининград','Europe/Berlin':'Берлин',
    'Europe/London':'Лондон','Asia/Yekaterinburg':'Екатеринбург','Asia/Novosibirsk':'Новосибирск',
    'Asia/Irkutsk':'Иркутск','Asia/Vladivostok':'Владивосток',UTC:'UTC'};
  function selectZone(value) {
    get('digest-zone').replaceChildren();
    const all = {...zones};
    if (!all[value]) all[value] = value.split('/').at(-1).replaceAll('_',' ');
    for (const [zone,label] of Object.entries(all)) get('digest-zone').append(new Option(label,zone));
    get('digest-zone').value = value;
  }
  const status = text => { get('digest-settings-status').textContent = text; };
  function reset() {
    generation++; known = false; get('digest-settings-save').disabled = true;
    get('digest-morning').checked = get('digest-evening').checked = false;
    get('digest-morning-time').value = '09:00'; get('digest-evening-time').value = '20:00';
    selectZone('Europe/Moscow'); status('');
  }
  async function load() {
    const ticket = ++generation, account = epoch;
    known = false; get('digest-settings-save').disabled = true; status('Загружаем настройки…');
    try {
      const row = await api('/api/v1/telegram/digest-settings');
      if (ticket !== generation || account !== epoch || get('workspace').hidden || !get('telegram-dialog').open) return;
      get('digest-morning').checked = row.morning_enabled; get('digest-evening').checked = row.evening_enabled;
      get('digest-morning-time').value = row.morning_time; get('digest-evening-time').value = row.evening_time;
      selectZone(row.configured ? row.timezone : Intl.DateTimeFormat().resolvedOptions().timeZone || row.timezone);
      known = true; get('digest-settings-save').disabled = false;
      status(row.telegram_available ? 'Рассылка включается только после сохранения этих настроек.' : 'Сначала подключите Telegram и разрешите уведомления.');
    } catch (error) { if (ticket === generation && account === epoch) status(error.message); }
  }
  get('digest-settings-form').onsubmit = async event => {
    event.preventDefault(); if (!known || get('workspace').hidden) return;
    const ticket = ++generation, account = epoch;
    get('digest-settings-save').disabled = true;
    const body = {morning_enabled:get('digest-morning').checked,evening_enabled:get('digest-evening').checked,
      morning_time:get('digest-morning-time').value,evening_time:get('digest-evening-time').value,timezone:get('digest-zone').value.trim()};
    try {
      const row = await api('/api/v1/telegram/digest-settings',{method:'PATCH',body:JSON.stringify(body)});
      if (ticket !== generation || account !== epoch || get('workspace').hidden) return;
      status(row.morning_enabled || row.evening_enabled ? 'Сохранено. Следующий обзор придёт в выбранное время, если есть заметки или задачи.' : 'Ежедневные обзоры выключены.');
    } catch (error) { if (ticket === generation && account === epoch && !get('workspace').hidden) status(error.message); }
    finally { if (ticket === generation && account === epoch && !get('workspace').hidden) get('digest-settings-save').disabled = false; }
  };
  new MutationObserver(() => { if (get('telegram-dialog').open && !get('workspace').hidden) load(); else { generation++; known = false; } })
    .observe(get('telegram-dialog'),{attributes:true,attributeFilter:['open']});
  get('refresh-links').addEventListener('click',load);
  new MutationObserver(() => { if (get('workspace').hidden) reset(); }).observe(get('workspace'),{attributes:true,attributeFilter:['hidden']});
  reset();
})();
