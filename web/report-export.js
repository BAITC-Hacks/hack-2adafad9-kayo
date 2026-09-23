(function () {
  'use strict';
  const utf8 = new TextEncoder();
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const number = (value, digits = 1) => finite(value) ? value.toLocaleString('ru-RU', { maximumFractionDigits: digits }) : '—';
  const percent = value => finite(value) ? number(value * 100) + ' %' : '—';
  const xml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[char])).replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '');
  const utc = value => value ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z') : null;
  const local = value => {
    const stamp = utc(value);
    return stamp && Number.isFinite(stamp.getTime()) ? new Date(stamp.getTime() + 5 * 3600000).toISOString().slice(0, 16).replace('T', ' ') : '—';
  };
  const localDate = value => value ? new Date(utc(value).getTime() + 5 * 3600000) : null;

  function selection() {
    const current = window.getWindReportState ? window.getWindReportState() : { payload: window.DASHBOARD_DATA };
    const payload = current.payload || {};
    const forecasts = payload.forecast || [];
    const issued = current.issueTime || forecasts.map(row => row.issue_time).filter(Boolean).sort().pop();
    const all = document.getElementById('exportScope')?.value === 'all';
    const rows = forecasts.filter(row => all || row.issue_time === issued)
      .slice().sort((a, b) => String(a.issue_time).localeCompare(String(b.issue_time)) || a.lead_h - b.lead_h || String(a.time).localeCompare(String(b.time)) || String(a.turbine).localeCompare(String(b.turbine)));
    if (!rows.length) throw new Error('Для выбранного выпуска ещё нет прогноза.');
    const journal = (payload.agent_log || []).filter(row => all || row.issue_time === issued);
    return { payload, rows, journal, issued, all, economics: current.economics, title: all ? 'Весь период' : 'Выпуск ' + local(issued) + ' (UTC+5)' };
  }

  // OOXML упаковывается без сжатия: выгрузка работает офлайн и не требует CDN.
  const crcTable = Array.from({ length: 256 }, (_, index) => {
    let crc = index;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    return crc >>> 0;
  });
  function zip(files) {
    const chunks = [], directory = [];
    let offset = 0, dirSize = 0;
    for (const [path, source] of Object.entries(files)) {
      const name = utf8.encode(path), bytes = utf8.encode(source);
      let crc = 0xffffffff;
      for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
      crc = (crc ^ 0xffffffff) >>> 0;
      const header = new Uint8Array(30), h = new DataView(header.buffer);
      h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x800, true);
      h.setUint16(12, 0x21, true); h.setUint32(14, crc, true); h.setUint32(18, bytes.length, true);
      h.setUint32(22, bytes.length, true); h.setUint16(26, name.length, true);
      chunks.push(header, name, bytes);
      const central = new Uint8Array(46), c = new DataView(central.buffer);
      c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true);
      c.setUint16(8, 0x800, true); c.setUint16(14, 0x21, true); c.setUint32(16, crc, true);
      c.setUint32(20, bytes.length, true); c.setUint32(24, bytes.length, true); c.setUint16(28, name.length, true);
      c.setUint32(42, offset, true);
      directory.push(central, name); dirSize += central.length + name.length;
      offset += header.length + name.length + bytes.length;
    }
    const end = new Uint8Array(22), e = new DataView(end.buffer), count = Object.keys(files).length;
    e.setUint32(0, 0x06054b50, true); e.setUint16(8, count, true); e.setUint16(10, count, true);
    e.setUint32(12, dirSize, true); e.setUint32(16, offset, true);
    return new Blob([...chunks, ...directory, end], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
  }
  function column(index) {
    let label = '';
    for (index++; index; index = Math.floor((index - 1) / 26)) label = String.fromCharCode(65 + (index - 1) % 26) + label;
    return label;
  }
  function sheet(headers, rows, widths, percentages = [], wrapped = []) {
    const grid = [headers, ...rows].map((row, ri) => {
      const lines = Math.max(1, ...wrapped.map(ci => Math.ceil(String(row[ci] ?? '').length / (widths[ci] - 2))));
      const height = ri === 0 ? 32 : Math.min(150, Math.max(22, lines * 15));
      return '<row r="' + (ri + 1) + '" ht="' + height + '" customHeight="1">' + row.map((value, ci) => {
      const ref = column(ci) + (ri + 1), style = ri === 0 ? 1 : percentages.includes(ci) ? 2 : 0;
      if (value && typeof value === 'object' && value.formula) return '<c r="' + ref + '" s="5"><f>' + xml(value.formula) + '</f><v>' + value.value + '</v></c>';
      if (value instanceof Date && Number.isFinite(value.getTime())) return '<c r="' + ref + '" s="4"><v>' + (value.getTime() / 86400000 + 25569) + '</v></c>';
      return finite(value) ? '<c r="' + ref + '" s="' + style + '"><v>' + value + '</v></c>'
        : '<c r="' + ref + '" s="' + (ri === 0 ? 1 : wrapped.includes(ci) ? 3 : 0) + '" t="inlineStr"><is><t xml:space="preserve">' + xml(value) + '</t></is></c>';
      }).join('') + '</row>';
    }).join('');
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'
      + widths.map((width, i) => '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + width + '" customWidth="1"/>').join('')
      + '</cols><sheetData>' + grid + '</sheetData><autoFilter ref="A1:' + column(headers.length - 1) + (rows.length + 1) + '"/><pageMargins left="0.3" right="0.3" top="0.4" bottom="0.4" header="0.2" footer="0.2"/><pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>';
  }
  function notes(report) {
    const p = report.payload;
    return [
      ['Площадка', p.site?.name || 'ВЭС'], ['Выбор', report.title],
      ['Доступные часы', report.all ? 'Все часы периода, все доступные выпуски.' : new Set(report.rows.map(row => row.time)).size + ' из 48 часов. На границе периода выпуск может быть неполным.'],
      ['Создано (UTC)', p.generated_at || ''],
      ['Валидация (UTC)', (p.validation_period?.start || '—') + ' — ' + (p.validation_period?.end || '—')],
      ['Единицы мощности', 'Доля номинала. 1 = 100 %. Установленная мощность и тариф станции не заданы.'],
      ['Время', 'time и issue_time — UTC; местное время — UTC+5.'],
      ['Периоды', 'lead_h=24: следующие 1–24 часа; lead_h=48: следующие 25–48 часов.'],
      ['Неопределённость', 'P10–P90 — целевой 80-процентный диапазон для отдельной турбины и часа; гарантий нет.'],
      ['Покрытие на валидации, все часы', percent(p.coverage)],
      ['Погодный источник', 'Open-Meteo Previous Runs API, ICON / GFS / ECMWF'],
      ['Доступность погоды', p.forecast_protocol?.availability_assumption || 'См. README соответствующей версии.'],
      ['Горизонт погоды', '48/72 часа от целевого часа назад; запас reference time до выпуска составляет 24–47 часов.'],
      ['Февраль', 'Фактическая выработка февраля отсутствует; метрики относятся только к периоду валидации.'],
      ['Область метрик', 'Ошибки — часы без ограничения выдачи. Покрытие P10–P90 — все часы с фактом. Полные метрики — forecasts/metrics.csv.'],
      ['Данные', 'Прогноз и метрики из web/data.json текущего прогона. Экспорт включает обе турбины.']
    ];
  }
  function excel(report) {
    const ours = (report.payload.metrics || []).filter(row => row.model === 'наше решение');
    const metric = (lead, key) => ours.find(row => row.lead_h === lead)?.[key];
    const sheets = [
      ['Сводка', ['Показатель', 'Следующие 1–24 ч', 'Следующие 25–48 ч'], [
        ['Выбранный период', report.title, 'Обе турбины'],
        ['Средняя ошибка / номинал (nMAE)', metric(24, 'nmae'), metric(48, 'nmae')],
        ['Среднеквадратичная ошибка (nRMSE)', metric(24, 'nrmse'), metric(48, 'nrmse')],
        ['Снижение MAE против базы (скилл)', metric(24, 'skill'), metric(48, 'skill')],
        ['Смещение / номинал', metric(24, 'bias'), metric(48, 'bias')],
        ['Что означают показатели', 'Точность на отложенном окне валидации, часы без ограничения выдачи.', 'Фактической выработки февраля нет.'],
        ['База сравнения', 'Прогноз «как вчера» при ежедневном доступе к факту.', 'Агент получает факт до начала месяца.'],
        ['Как читать файл', 'Почасовые значения — лист «Прогноз».', 'Источники — «Описание», решения — «Журнал».']
      ], [42, 43, 43], [1, 2], [0, 1, 2]],
      ['Прогноз', ['Час UTC+5', 'Турбина', 'P10', 'P50', 'P90', 'Ветер 100 м, м/с', 'Направление, °', 'Выпуск UTC+5', 'Час UTC', 'Выпуск UTC', 'Период, ч', 'До часа, ч', 'Горизонт погоды, ч', 'Reference time UTC'],
        report.rows.map(r => [localDate(r.time), r.turbine, r.p10, r.p50, r.p90, r.wind_speed_100m, r.wind_direction_100m, localDate(r.issue_time), utc(r.time), utc(r.issue_time), r.lead_h, r.effective_lead_h, r.weather_lead_h, utc(r.weather_reference_time)]), [22, 12, 14, 14, 14, 20, 20, 22, 22, 22, 14, 14, 22, 24], [2, 3, 4]],
      ['Точность', ['Модель', 'Период, ч', 'nMAE', 'nRMSE', 'Смещение', 'Скилл (по nMAE)'], (report.payload.metrics || []).map(r => [r.model, r.lead_h, r.nmae, r.nrmse, r.bias, r.skill]), [40, 16, 16, 16, 16, 24], [2, 3, 4, 5]],
      ['Описание', ['Параметр', 'Значение'], notes(report), [32, 100], [], [1]],
      ['Журнал', ['Выпуск UTC', 'Шаг', 'Статус', 'Длительность, мс', 'Объяснение'], report.journal.map(r => [utc(r.issue_time), r.step, r.status, r.duration_ms, r.text]), [24, 20, 18, 22, 100], [], [4]]
    ];
    if (report.economics) {
      const scenario = report.economics;
      const baseline = (report.payload.metrics || []).find(row => row.model === 'персистентность' && row.lead_h === 24);
      const oursMae = metric(24, 'nmae');
      const normalizedEnergy = (report.payload.forecast || []).filter(row => row.lead_h === 24 && finite(row.p50)).reduce((sum, row) => sum + row.p50, 0);
      sheets.splice(2, 0, ['Экономика', ['Параметр', 'Значение', 'Единица / основание'], [
        ['Мощность одной турбины', scenario.capacityMw, 'МВт — допущение пользователя'],
        ['Условная цена ошибки', scenario.penaltyPerMwh, '₸/МВт·ч — допущение пользователя'],
        ['Длительность всего периода', scenario.hours, 'часов; экономика всего периода, независимо от выбора выпуска'],
        ['Число турбин', scenario.turbines, 'по данным прогноза'],
        ['nMAE базы', baseline?.nmae, 'доля номинала; валидация без простоев'],
        ['nMAE агента', oursMae, 'доля номинала; валидация без простоев'],
        ['Цена ошибки «как вчера»', {formula:'B2*B3*B4*B5*B6',value:scenario.baselineCost}, '₸ за весь период — оценка'],
        ['Цена ошибки с агентом', {formula:'B2*B3*B4*B5*B7',value:scenario.agentCost}, '₸ за весь период — оценка'],
        ['Снижение условных затрат', {formula:'B8-B9',value:scenario.savings}, '₸ за весь период — оценка'],
        ['Сумма P50 всех турбин, горизонт 24 ч', normalizedEnergy, 'нормированных турбино-часов; горизонт 48 ч не дублируется'],
        ['Ожидаемая энергия всего периода', {formula:'B2*B11',value:scenario.energyMwh}, 'МВт·ч — при принятой мощности'],
        ['Ограничения', 'Сценарий, не фактическая экономия.', 'Средняя ошибка валидации перенесена на длительность прогноза. Реальные мощности, договор и тарифы станции не предоставлены.'],
        ['База сравнения', 'Получает ежедневный факт.', 'Агент работает без нового факта с начала месяца.']
      ], [44, 32, 82], [], [0, 1, 2]]);
    }
    const relNS = 'http://schemas.openxmlformats.org/package/2006/relationships';
    const files = {
      '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + sheets.map((_, i) => '<Override PartName="/xl/worksheets/sheet' + (i + 1) + '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>').join('') + '</Types>',
      '_rels/.rels': '<Relationships xmlns="' + relNS + '"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + sheets.map((s, i) => '<sheet name="' + xml(s[0]) + '" sheetId="' + (i + 1) + '" r:id="rId' + (i + 1) + '"/>').join('') + '</sheets></workbook>',
      'xl/_rels/workbook.xml.rels': '<Relationships xmlns="' + relNS + '">' + sheets.map((_, i) => '<Relationship Id="rId' + (i + 1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet' + (i + 1) + '.xml"/>').join('') + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/styles.xml': '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF23684C"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1"/></xf><xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    };
    files['xl/styles.xml'] = files['xl/styles.xml'].replace('<fonts count="2">', '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/></numFmts><fonts count="2">').replace('<cellXfs count="3">', '<cellXfs count="6">').replace('</cellXfs>', '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>');
    sheets.forEach((s, i) => { files['xl/worksheets/sheet' + (i + 1) + '.xml'] = sheet(s[1], s[2], s[3], s[4], s[5]); });
    const url = URL.createObjectURL(zip(files)), link = document.createElement('a');
    link.href = url; link.download = 'wind-forecast-' + (report.all ? 'all' : String(report.issued).replace(/[:T]/g, '-')) + '.xlsx';
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  document.addEventListener('click', async event => {
    const button = event.target.closest('#exportExcel, #exportPdf');
    if (!button) return;
    const label = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      const report = selection();
      if (button.id === 'exportExcel') excel(report);
      else {
        button.textContent = 'Готовим PDF…';
        if (!window.WindPDF) throw new Error('Модуль PDF ещё загружается. Повторите через несколько секунд.');
        await window.WindPDF.download(report);
      }
      document.getElementById('report-export-status')?.remove();
    } catch (error) {
      let status = document.getElementById('report-export-status');
      if (!status) { status = element('p'); status.id = 'report-export-status'; status.setAttribute('role', 'status'); button.parentNode.append(status); }
      status.textContent = error.message;
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.textContent = label;
    }
  });
}());
