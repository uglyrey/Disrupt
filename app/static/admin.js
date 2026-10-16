'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const API = document.body.dataset.review === 'true' ? '/review-api/v1' : '/admin-api/v1';
  const nf = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const moscowDay = date => new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Moscow'}).format(date);
  const shiftDay = (day, delta) => new Date(Date.parse(day + 'T12:00:00Z') + delta * 86400000).toISOString().slice(0, 10);
  const timestamp = value => {
    if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) throw new Error('schema');
    return new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short'}).format(new Date(value)) + ' МСК';
  };
  const count = value => {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error('schema');
    return nf.format(value);
  };
  const number = value => {
    if (value === null) return 'Не измерено';
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new Error('schema');
    return nf.format(value);
  };
  const text = value => {
    if (value === null) return '—';
    if (typeof value !== 'string') throw new Error('schema');
    return value;
  };
  const money = value => {
    if (value === null) return 'Не рассчитано';
    if (typeof value !== 'string' || !/^\d+(\.\d+)?$/.test(value) || !Number.isFinite(Number(value))) throw new Error('schema');
    return (Number(value) > 0 && Number(value) < 0.01 ? '< 0,01' : nf.format(Number(value))) + ' ₽';
  };
  const percent = value => {
    count(value.numerator); count(value.denominator);
    if (value.numerator > value.denominator || (value.denominator === 0) !== (value.value === null)) throw new Error('schema');
    if (value.value !== null && (value.value > 100 || Math.abs(value.value - 100 * value.numerator / value.denominator) > 0.000001)) throw new Error('schema');
    return value.value === null ? 'Нет наблюдений' : number(value.value) + '%';
  };
  const node = (tag, content, className) => {
    const el = document.createElement(tag);
    if (content !== undefined) el.textContent = content;
    if (className) el.className = className;
    return el;
  };
  function cards(id, items) {
    const nodes = items.map(([label, value, hint, state]) => {
      const el = node('article', undefined, 'metric' + (state ? ' metric--' + state : ''));
      el.append(node('h3', label), node('p', value));
      if (hint) el.append(node('small', hint));
      return el;
    });
    $(id).replaceChildren(...nodes);
  }
  function table(id, headings, rows) {
    if (!rows.length) { $(id).replaceChildren(node('p', 'За этот период данных нет.')); return; }
    const el = node('table'), head = node('thead'), tr = node('tr'), body = node('tbody');
    for (const label of headings) { const th = node('th', label); th.scope = 'col'; tr.append(th); }
    head.append(tr);
    for (const row of rows) { const r = node('tr'); for (const cell of row) r.append(node('td', cell)); body.append(r); }
    el.append(head, body); $(id).replaceChildren(el);
  }
  const ratio = p => `${count(p.numerator)} из ${count(p.denominator)}`;
  let generation = 0, controller = null, applied = null, next = null, offsets = [], offset = 0;
  let exporting = false;
  function clear() {
    applied = null; next = null;
    $('dashboard').hidden = true;
    for (const id of ['cards','daily','funnel','retention','costs','token-costs','accounting-status','cost-context','quality','usage','budget-cards','budget-status','contest-cards','contest-method','contest-days','traffic-sources','contest-period-status','contest-period-cards']) $(id).replaceChildren();
    $('export').disabled = true; $('next').disabled = true; $('previous').disabled = true;
  }
  function status(message, error = false) { $('status').textContent = message; $('status').className = error ? 'error' : ''; }
  function filters() {
    const from = $('from').value, to = $('to').value, channel = $('channel').value, source = $('source').value.trim() || 'all';
    const validDay = day => /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(Date.parse(day)) && new Date(day).toISOString().slice(0,10) === day;
    if (!validDay(from) || !validDay(to) || from > to || !['all','web','telegram'].includes(channel) || source.length > 100) throw new Error('filters');
    return {from, to, channel, source};
  }
  function render(data, slice, currentOffset) {
    const contest = data.contest;
    if (contest) {
      cards('contest-cards', [['Использовали хотя бы раз к концу периода',count(contest.real_users), 'Сохранили хотя бы одну запись. Просмотр результата не обязателен.'],
        ['Средний DAU за выбранные дни',number(contest.average_dau)],
        ['Учтённые обращения на DAU · среднесуточно',number(contest.average_calls_per_dau)]]);
      $('contest-method').textContent = 'Использование сервиса: сохранить запись хотя бы один раз. Просмотр и успешная обработка ИИ не обязательны. ' +
        'Счётчик накопительный до конечной даты, веб и Telegram вместе. Канал фильтрует активность по дням. Регистрации без записи и тестовые аккаунты исключены. ' +
        'Числитель и знаменатель используют одну группу реальных пользователей. Нулевые дни входят в среднее. ' +
        (contest.preliminary ? 'Период ещё не завершён, показатели предварительные. ' : '') +
        (contest.component_method_pending ? 'Часть записанных этапов пока не включена в зачёт до согласования методики компонентов. ' : '') +
        'Версия ' + text(contest.method_version) + '.';
      table('contest-days',['Дата','DAU','Модельные вызовы','Учтённые обращения','Все записанные этапы','Обращения / DAU'],
        contest.daily.map(day=>[text(day.date),count(day.dau),count(day.model_calls),count(day.calls),count(day.recorded_calls),number(day.calls_per_dau)]));
      table('traffic-sources',['Источник','Регистрации к концу периода','Использовали хотя бы раз к концу периода'],
        contest.sources.map(row=>[text(row.source),count(row.registrations),count(row.real_users)]));
    }
    const budget = data.budget;
    if (budget && budget.configured) {
      if (budget.currency !== 'RUB') throw new Error('schema');
      cards('budget-cards', [['Выделено',money(budget.total)],['Потрачено',money(budget.spent)],
        ['Осталось на ключе',money(budget.remaining)],['Резерв на следующие этапы',money(budget.reserve)],
        ['Доступно для пилота',money(budget.pilot_remaining)]]);
      $('budget-status').textContent = 'Весь ключ за всё время, включая тестовые аккаунты и STT. Даты фильтра не влияют на баланс. ' +
        (budget.checked_at ? 'Данные шлюза на ' + timestamp(budget.checked_at) + '. ' : 'Баланс ещё не получен. ') +
        (!budget.fresh ? 'Данные устарели или недоступны. ' : '') +
        (budget.processing_paused ? 'Новые обращения к модели приостановлены для защиты бюджета. ' : '') +
        'Модель ' + text(budget.model) + ' · ' + count(budget.daily_unit_limit) + ' единиц в день.';
    } else {
      $('budget-status').textContent = 'Получение баланса ключа ещё не подключено.';
    }
    if (![data.daily, data.funnel, data.usage].every(Array.isArray) || data.usage.length > 50) throw new Error('schema');
    const c = data.cards, q = data.quality, costs = q.costs, r = data.retention;
    if (costs.currency !== 'RUB') throw new Error('schema');
    const stamp = timestamp(data.generated_at), lastFinished = shiftDay(moscowDay(new Date(data.generated_at)), -1);
    const dauDay = slice.to < lastFinished ? slice.to : lastFinished;
    const dauLabel = slice.from > lastFinished ? 'DAU текущего дня · предварительно' : 'DAU за ' + dauDay;
    cards('cards', [
      [dauLabel, count(c.dau)], ['Активные за период', count(c.unique_users)], ['Регистрации', count(c.registrations)],
      ['Новые пользователи', count(c.new_users)], ['Вернувшиеся пользователи', count(c.returning_users)],
      ['Активация с ИИ', percent(c.ai_activation), ratio(c.ai_activation)],
      ['Активация без ИИ', percent(c.manual_activation), ratio(c.manual_activation)],
      ['Сохранили запись', count(c.saved_users ?? 0), 'Уникальные пользователи за выбранный период'], ['Получили готовую заметку', count(c.ready_users ?? 0), 'Заметка создана за выбранный период'], ['Возвраты после активации', count(c.returns)],
    ]);
    $('activation-pending').textContent = `Ожидают завершения окна активации: ${count(c.activation_pending)}.`;
    table('daily', ['Дата','Регистрации','DAU','Новые','Вернувшиеся','Сессии'], data.daily.map(d => [text(d.date), count(d.registrations), count(d.dau), count(d.new_users), count(d.returning_users), count(d.session_count)]));
    const labels = {registered:'Зарегистрировались',capture_saved:'Сохранили запись',note_opened:'Открыли результат',structure_checked:'Проверили структуру',returned:'Вернулись'};
    table('funnel', ['Шаг','Пользователи','Конверсия'], data.funnel.map(f => [labels[f.step] || text(f.step), count(f.users), percent(f.conversion) + ' · ' + ratio(f.conversion)]));
    cards('retention', ['d1','d7'].map(key => [key.toUpperCase(), r[key].value === null ? 'Пока рано' : percent(r[key]),
      r[key].value === null ? (r['pending_' + key] ? 'Окно наблюдения ещё не завершилось' : 'Нет подходящих регистраций') : ratio(r[key])]));
    $('retention-pending').textContent = `Окно ещё не завершилось: D1 — ${count(r.pending_d1)}, D7 — ${count(r.pending_d7)}.`;
    const measured = (label, full, known, covered, total, format) => {
      count(covered); count(total);
      if (covered > total || (covered === 0 && total > 0 && known !== null)) throw new Error('schema');
      const incomplete = covered < total;
      return [label, format(full === null ? known : full), total === 0 ? 'За период запросов не было' :
        `Учтено ${count(covered)} из ${count(total)} запросов${incomplete && covered ? ' · неполная сумма' : ''}`,
        incomplete ? 'incomplete' : 'complete'];
    };
    const costCard = (kind, label) => {
      const covered = costs[kind + '_cost_calls'], total = costs[kind + '_calls'];
      if (!covered && total && costs['reference_' + kind + '_cost'] !== null) {
        const referenceCount = costs['reference_' + kind + '_calls']; count(referenceCount);
        if (referenceCount > total) throw new Error('schema');
        return [label + ' · оценка', '≈ ' + money(costs['reference_' + kind + '_cost']),
          `Тариф Cloud.ru · ${count(referenceCount)} из ${count(total)} запросов · списание шлюза не подтверждено`, 'estimate'];
      }
      return measured(label, costs[kind + '_cost'], covered || !total ? costs['known_' + kind + '_cost'] : null, covered, total, money);
    };
    const per = (label, field, denominator, hint) => costs[field] !== null
      ? [label,money(costs[field]),hint]
      : costs['reference_' + field] !== null ? [label + ' · оценка','≈ ' + money(costs['reference_' + field]),'По учтённой части · тариф Cloud.ru','estimate']
      : [label,'Не рассчитано',denominator ? 'Не хватает измерений стоимости' : 'Нет наблюдений для расчёта'];
    const tokenCards = ['input_tokens','output_tokens','cache_tokens'].map((key, index) => measured(
      ['Входные токены','Выходные токены','Из кэша'][index], costs[key], costs['known_' + key], costs[key + '_calls'], costs.llm_calls, number));
    cards('costs', [costCard('llm','Текстовая модель'), costCard('stt','Распознавание речи'),
      per('На пользователя','cost_per_user',costs.user_count,'Все расходы / активные пользователи'),
      per('На сессию','cost_per_session',costs.session_count,'Все расходы / сессии'),
      per('LLM на DAU','llm_cost_per_dau',costs.dau_sum,'Стоимость LLM / сумма дневных DAU')]);
    cards('token-costs', [...tokenCards,
      measured('Минуты аудио',costs.stt_minutes,costs.known_stt_minutes,costs.stt_seconds_calls,costs.stt_calls,
        value => value === null ? 'Не измерено' : number(Number(text(value)))),
      ['Запросы LLM',count(costs.llm_calls)],['Запросы STT',count(costs.stt_calls)]]);
    const missingCosts = count(costs.cost_missing_calls), missingTokens = count(costs.token_usage_missing_calls);
    const billing = costs.cost_missing_calls
      ? `Стоимость не получена для ${missingCosts} операций. Для расхода в рублях нужен подтверждённый тариф нашего шлюза. Неизвестные списания не считаются нулевыми.`
      : costs.llm_calls + costs.stt_calls ? 'Стоимость учтена для всех операций за период.' : 'За этот период обращений к модели не было.';
    const usageNote = costs.token_usage_missing_calls ? ` Неполные данные о токенах у ${missingTokens} запросов LLM. Ниже показана сохранённая часть.` : '';
    const legacyNote = costs.legacy_calls ? ` До включения учёта выполнено ${count(costs.legacy_calls)} операций; их токены задним числом не восстановить.` : '';
    const referenceCount = costs.reference_llm_calls + costs.reference_stt_calls; count(referenceCount);
    const comparison = referenceCount ? ' Карточки со знаком ≈ показывают справочную оценку по тарифам Cloud.ru на ' + text(costs.reference_pricing_date) + '. Входные токены оценены без скидки за кэш. Это не баланс ключа и не подтверждённое списание.' : '';
    $('accounting-status').replaceChildren(node('strong', costs.cost_missing_calls ? 'Измерения и справочная оценка' : 'Учёт за выбранный период'), node('p', billing + usageNote + legacyNote + comparison));
    if (referenceCount) {
      const link = node('a','Публичные тарифы Cloud.ru');
      link.href = 'https://cloud.ru/products/evolution-ai-factory/catalog-foundation-models';
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      $('accounting-status').append(link);
    }
    $('accounting-status').classList.toggle('is-incomplete',costs.cost_missing_calls > 0 || costs.token_usage_missing_calls > 0);
    $('cost-context').textContent = `В расчёте ${count(costs.user_count)} пользователей · ${count(costs.session_count)} сессий · сумма дневных DAU ${count(costs.dau_sum)}.`;
    cards('quality', [
      ['Успешные обработки ИИ',count(q.ai_succeeded)], ['Ошибки ИИ',count(q.ai_failed)], ['Успешность ИИ',percent(q.ai_success_rate),ratio(q.ai_success_rate)],
      ['Исправленные заметки',count(q.edited_notes)], ['Обработка p95 · мс',number(q.processing_p95_ms)],
      ['Напоминания отправлены',count(q.reminder_sent)], ['Доставка заблокирована',count(q.reminder_blocked)],
      ['Ошибки доставки',count(q.reminder_failed)], ['Неизвестный исход доставки',count(q.reminder_unknown)], ['Напоминания открыты',count(q.reminder_opened)],
    ]);
    table('usage', ['Время МСК','Пользователь','Сессия','Операция','Канал','Тип','Модель','Статус','Входные токены','Выходные','Кэш','Аудио · с','Списание · ₽','Оценка Cloud.ru','Тариф списания','Задержка · мс'], data.usage.map(u => [timestamp(u.occurred_at),text(u.user_pseudonym),text(u.session_pseudonym),text(u.operation_pseudonym),text(u.channel),text(u.kind),text(u.model),text(u.status),u.kind === 'llm' ? number(u.input_tokens) : '—',u.kind === 'llm' ? number(u.output_tokens) : '—',u.kind === 'llm' ? number(u.cache_tokens) : '—',u.kind === 'stt' ? number(u.stt_seconds) : '—',u.cost === null ? 'Не передано' : money(u.cost),u.reference_cost === null ? '—' : '≈ ' + money(u.reference_cost),text(u.tariff_version),number(u.latency_ms)]));
    $('snapshot').textContent = `${slice.from} — ${slice.to} · ${slice.channel === 'all' ? 'Все каналы' : slice.channel} · Источник ${slice.source} · Сформировано ${stamp}`;
    $('page').textContent = data.usage.length ? `Операции ${currentOffset + 1}–${currentOffset + data.usage.length}` : 'Нет операций';
  }
  const errorMessage = error => ({401:'Сессия завершилась. Войдите снова.',403:'Доступ запрещён.',404:'API статистики недоступен. Данные не загружены.',503:'Статистика временно недоступна. Попробуйте позже.',filters:'Проверьте даты: начало периода не должно быть позже окончания.'})[error.status || error.message] || 'Не удалось загрузить данные. Повторите запрос.';
  const expire = error => { if (error.status === 401 && window.adminSession) window.adminSession.expired(); };
  $('contest-period-load').addEventListener('click', async () => {
    if (!window.adminSession?.ready) return;
    const serial = generation, abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000);
    $('contest-period-load').disabled = true;
    try {
      const response = await fetch(API + '/contest',{credentials:'same-origin',cache:'no-store',redirect:'error',signal:abort.signal,headers:{Accept:'application/json'}});
      if (!response.ok) throw {status:response.status};
      const value = await response.json();
      if (serial !== generation) return;
      if (serial !== generation || !window.adminSession?.ready) return;
      cards('contest-period-cards',[['Средний DAU',number(value.average_dau)],['Обращения / DAU',number(value.average_calls_per_dau)],
        ['Дней DAU ≥ 1000',count(value.days_dau_1000) + ' из ' + count(value.days)],
        ['Дней DAU ≥ 2500',count(value.days_dau_2500) + ' из ' + count(value.days)]]);
      $('contest-period-status').textContent = text(value.start) + ' - ' + text(value.end) + '. Все источники и каналы. ' +
        (value.preliminary ? 'Период не завершён, итоговый результат ещё не подтверждён.' : 'Период завершён. Пороги проверяет организатор.');
    } catch(error) {
      if (serial !== generation) return;
      if (error.status === 401 || error.status === 403) { clear(); expire(error); }
      else if (serial === generation && window.adminSession?.ready) $('contest-period-status').textContent = 'Не удалось загрузить зачётный период. Повторите позже.';
    } finally { clearTimeout(timer); $('contest-period-load').disabled = false; }
  });
  async function load(targetOffset = 0, history = [], slice) {
    const serial = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    const active = controller, timer = setTimeout(() => active.abort(), 15000);
    clear(); $('apply').disabled = true; $('dashboard').setAttribute('aria-busy','true'); status('Загружаем статистику…');
    try {
      slice = slice || filters();
      const params = new URLSearchParams({...slice, usage_limit:'50',usage_offset:String(targetOffset)});
      const response = await fetch(API + '/summary?' + params, {credentials:'same-origin',cache:'no-store',redirect:'error',signal:active.signal,headers:{Accept:'application/json'}});
      if (!response.ok) throw {status:response.status};
      const data = await response.json();
      if (serial !== generation) return;
      const cursor = response.headers.get('X-Next-Usage-Offset');
      if (cursor !== null && (!/^\d+$/.test(cursor) || !Number.isSafeInteger(Number(cursor)) || Number(cursor) <= targetOffset)) throw new Error('schema');
      render(data, slice, targetOffset);
      applied = {...slice}; offset = targetOffset; offsets = history; next = cursor === null ? null : Number(cursor);
      $('dashboard').hidden = false; $('export').disabled = false; $('next').disabled = next === null; $('previous').disabled = !offsets.length;
      status('Данные загружены.');
    } catch (error) {
      if (serial !== generation) return;
      clear(); status(errorMessage(error), true); expire(error);
    } finally {
      clearTimeout(timer);
      if (serial === generation) { $('apply').disabled = false; $('dashboard').setAttribute('aria-busy','false'); }
    }
  }
  $('filters').addEventListener('submit', event => {event.preventDefault(); load();});
  for (const button of document.querySelectorAll('[data-period]')) button.addEventListener('click', () => {
    const today = moscowDay(new Date());
    $('to').value = today;
    $('from').value = button.dataset.period === 'today' ? today : shiftDay(today, 1 - Number(button.dataset.period));
    load();
  });
  $('filters').addEventListener('input', () => {
    generation++; if (controller) controller.abort(); clear(); $('apply').disabled = false;
    status('Фильтры изменены. Нажми «Показать».');
  });
  $('next').addEventListener('click', () => {if (next !== null && applied) load(next, [...offsets,offset], applied);});
  $('previous').addEventListener('click', () => {if (offsets.length && applied) load(offsets.at(-1), offsets.slice(0,-1), applied);});
  $('export').addEventListener('click', async () => {
    if (!applied || exporting) return;
    exporting = true; $('export').disabled = true;
    const serial = generation, slice = {...applied}, abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await fetch(API + '/export?' + new URLSearchParams(slice), {credentials:'same-origin',cache:'no-store',redirect:'error',signal:abort.signal,headers:{Accept:'text/csv'}});
      if (!response.ok) throw {status:response.status};
      if (!(response.headers.get('Content-Type') || '').toLowerCase().startsWith('text/csv')) throw new Error('schema');
      const blob = await response.blob();
      if (serial !== generation) return;
      const url = URL.createObjectURL(blob), link = node('a');
      link.href = url; link.download = `beresta-${slice.from}-${slice.to}.csv`; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000); status('CSV скачан для выбранного периода.');
    } catch (error) {
      if (serial !== generation) return;
      if ([401,403].includes(error.status)) clear();
      status(errorMessage(error), true); expire(error);
    } finally { clearTimeout(timer); exporting = false; $('export').disabled = !applied; }
  });
  $('to').value = shiftDay(moscowDay(new Date()), -1);
  $('from').value = shiftDay($('to').value, -6);
  if (window.adminSession) {
    // Sign-in decides when data may be requested. Logout and expiry wipe everything shown.
    window.adminSession.onReady(() => load());
    window.adminSession.onLogout(() => { generation++; if (controller) controller.abort(); clear(); status(''); });
  } else {
    load();
  }
})();
