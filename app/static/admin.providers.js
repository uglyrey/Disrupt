'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const form = $('filters'), dashboard = $('dashboard'), state = $('providers-state');
  if (!form || !dashboard || !state) return;
  const nf = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const NAMES = {cloudru: 'Cloud.ru', openai: 'Внешний ИИ, резерв', mock: 'Имитация', injected: 'Тестовый сервис'};
  const stamp = value => new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short'}).format(new Date(value)) + ' МСК';
  const usd = value => {
    if (typeof value !== 'string' || !/^\d+(\.\d+)?$/.test(value)) throw new Error('schema');
    const number = Number(value);
    return (number > 0 && number < 0.01 ? '< 0,01' : nf.format(number)) + ' $';
  };
  const node = (tag, content, className) => {
    const el = document.createElement(tag);
    if (content !== undefined) el.textContent = content;
    if (className) el.className = className;
    return el;
  };
  const card = (label, value, hint, kind) => {
    const el = node('article', undefined, 'metric' + (kind ? ' metric--' + kind : ''));
    el.append(node('h3', label), node('p', value));
    if (hint) el.append(node('small', hint));
    return el;
  };
  function render(data) {
    const down = data.primary_state === 'down';
    const primaryHint = down
      ? 'С ' + stamp(data.primary_changed_at) + (data.next_probe_at ? '. Следующая проверка после ' + stamp(data.next_probe_at) : '')
      : 'Все задания идут сюда';
    const reserve = data.fallback_enabled
      ? card('Резерв', 'Включён', usd(data.fallback_spent_today_usd) + ' из ' + usd(data.fallback_cap_usd) + ' за сегодня')
      : card('Резерв', 'Выключен', 'Включается настройкой NOTES_FALLBACK_ENABLED');
    state.replaceChildren(card('Cloud.ru', down ? 'Недоступен' : 'Работает', primaryHint, down ? 'incomplete' : undefined), reserve);
    const names = [...new Set([...data.today, ...data.period].map(row => row.provider))];
    const find = (rows, name) => rows.find(row => row.provider === name);
    const rows = names.map(name => {
      const today = find(data.today, name), period = find(data.period, name);
      const tokens = row => row ? nf.format(row.input_tokens) + ' / ' + nf.format(row.output_tokens) : '0 / 0';
      const money = row => row ? usd(row.estimated_usd) : '0 $';
      return [NAMES[name] || name, nf.format(today ? today.calls : 0), tokens(today), money(today),
        nf.format(period ? period.calls : 0), tokens(period), money(period)];
    });
    const table = $('providers-table');
    if (!rows.length) { table.replaceChildren(node('p', 'За этот период вызовов нет.')); return; }
    const el = node('table'), head = node('thead'), tr = node('tr'), body = node('tbody');
    for (const label of ['Сервис', 'Сегодня, вызовы', 'Сегодня, токены (вход / выход)', 'Сегодня, ≈ $', 'Период, вызовы', 'Период, токены (вход / выход)', 'Период, ≈ $']) {
      const th = node('th', label); th.scope = 'col'; tr.append(th);
    }
    head.append(tr);
    for (const row of rows) { const r = node('tr'); for (const cell of row) r.append(node('td', cell)); body.append(r); }
    el.append(head, body);
    table.replaceChildren(el);
  }
  let generation = 0;
  async function load() {
    const from = $('from').value, to = $('to').value, note = $('providers-note');
    if (!from || !to) return;
    const mine = ++generation;
    try {
      const response = await fetch('/admin-api/v1/ai-providers?' + new URLSearchParams({from, to}), {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: {Accept: 'application/json'},
      });
      if (!response.ok) throw new Error('http');
      const data = await response.json();
      if (mine !== generation) return;
      render(data);
      note.textContent = 'Расход резерва оценён по токенам и ценам из настроек, это не счёт OpenAI. Данные на ' + stamp(data.generated_at) + '.';
    } catch (error) {
      if (mine !== generation) return;
      state.replaceChildren();
      $('providers-table').replaceChildren();
      note.textContent = 'Не удалось получить данные об ИИ-сервисах.';
    }
  }
  new MutationObserver(() => { if (!dashboard.hidden) load(); }).observe(dashboard, {attributes: true, attributeFilter: ['hidden']});
  form.addEventListener('submit', () => setTimeout(load, 0));
  if (!dashboard.hidden) load();
})();
