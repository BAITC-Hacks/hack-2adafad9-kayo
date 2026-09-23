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
    return { payload, rows, journal, issued, all, title: all ? 'Весь период' : 'Выпуск ' + local(issued) + ' (UTC+5)' };
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
  function sheet(headers, rows, widths, percentages = []) {
    const grid = [headers, ...rows].map((row, ri) => '<row r="' + (ri + 1) + '">' + row.map((value, ci) => {
      const ref = column(ci) + (ri + 1), style = ri === 0 ? 1 : percentages.includes(ci) ? 2 : 0;
      return finite(value) ? '<c r="' + ref + '" s="' + style + '"><v>' + value + '</v></c>'
        : '<c r="' + ref + '" s="' + (ri === 0 ? 1 : 0) + '" t="inlineStr"><is><t xml:space="preserve">' + xml(value) + '</t></is></c>';
    }).join('') + '</row>').join('');
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'
      + widths.map((width, i) => '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + width + '" customWidth="1"/>').join('')
      + '</cols><sheetData>' + grid + '</sheetData><autoFilter ref="A1:' + column(headers.length - 1) + (rows.length + 1) + '"/></worksheet>';
  }
  function notes(report) {
    const p = report.payload;
    return [
      ['Площадка', p.site?.name || 'ВЭС'], ['Выбор', report.title],
      ['Создано (UTC)', p.generated_at || ''],
      ['Валидация (UTC)', (p.validation_period?.start || '—') + ' — ' + (p.validation_period?.end || '—')],
      ['Единицы мощности', 'Доля номинала. 1 = 100 %. Установленная мощность и тариф станции не заданы.'],
      ['Время', 'time и issue_time — UTC; местное время — UTC+5.'],
      ['Периоды', 'lead_h=24: следующие 1–24 часа; lead_h=48: следующие 25–48 часов.'],
      ['Неопределённость', 'P10–P90 — целевой 80-процентный диапазон для отдельной турбины и часа; гарантий нет.'],
      ['Покрытие на валидации', percent(p.coverage)],
      ['Погодный источник', 'Open-Meteo Previous Runs API, ICON / GFS / ECMWF'],
      ['Доступность погоды', p.forecast_protocol?.availability_assumption || 'См. README соответствующей версии.'],
      ['Февраль', 'Фактическая выработка февраля отсутствует; метрики относятся только к периоду валидации.'],
      ['Область метрик', 'Часы без ограничения выдачи; полные метрики с простоями — forecasts/metrics.csv.'],
      ['Данные', 'Прогноз и метрики из web/data.json текущего прогона. Экспорт включает обе турбины.']
    ];
  }
  function excel(report) {
    const sheets = [
      ['Описание', ['Параметр', 'Значение'], notes(report), [32, 110]],
      ['Прогноз', ['Выпуск UTC', 'Час UTC', 'Час UTC+5', 'Турбина', 'Период, ч', 'До часа, ч', 'P10', 'P50', 'P90', 'Ветер 100 м, м/с', 'Направление, °', 'Возраст погоды, ч', 'Reference time UTC'],
        report.rows.map(r => [r.issue_time, r.time, local(r.time), r.turbine, r.lead_h, r.effective_lead_h, r.p10, r.p50, r.p90, r.wind_speed_100m, r.wind_direction_100m, r.weather_lead_h, r.weather_reference_time]), [22, 22, 22, 12, 14, 14, 14, 14, 14, 20, 20, 22, 24], [6, 7, 8]],
      ['Точность', ['Модель', 'Период, ч', 'nMAE', 'nRMSE', 'Смещение', 'Скилл (по nMAE)'], (report.payload.metrics || []).map(r => [r.model, r.lead_h, r.nmae, r.nrmse, r.bias, r.skill]), [40, 16, 16, 16, 16, 24], [2, 3, 4, 5]],
      ['Журнал', ['Выпуск UTC', 'Шаг', 'Статус', 'Длительность, мс', 'Объяснение'], report.journal.map(r => [r.issue_time, r.step, r.status, r.duration_ms, r.text]), [24, 20, 18, 22, 120]]
    ];
    const relNS = 'http://schemas.openxmlformats.org/package/2006/relationships';
    const files = {
      '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + sheets.map((_, i) => '<Override PartName="/xl/worksheets/sheet' + (i + 1) + '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>').join('') + '</Types>',
      '_rels/.rels': '<Relationships xmlns="' + relNS + '"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + sheets.map((s, i) => '<sheet name="' + xml(s[0]) + '" sheetId="' + (i + 1) + '" r:id="rId' + (i + 1) + '"/>').join('') + '</sheets></workbook>',
      'xl/_rels/workbook.xml.rels': '<Relationships xmlns="' + relNS + '">' + sheets.map((_, i) => '<Relationship Id="rId' + (i + 1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet' + (i + 1) + '.xml"/>').join('') + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/styles.xml': '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF23684C"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1"/></xf><xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    };
    sheets.forEach((s, i) => { files['xl/worksheets/sheet' + (i + 1) + '.xml'] = sheet(s[1], s[2], s[3], s[4]); });
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
  function table(headers, rows) {
    const node = element('table'), head = element('thead'), tr = element('tr'), body = element('tbody');
    headers.forEach(label => tr.append(element('th', label))); head.append(tr);
    rows.forEach(row => { const line = element('tr'); row.forEach(value => line.append(element('td', value))); body.append(line); });
    node.append(head, body); return node;
  }
  function chart(rows) {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 720 185'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Почасовой прогноз P50 двух турбин');
    const hours = [...new Set(rows.map(r => r.time))].sort();
    const draw = (tag, attrs, text) => { const node = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v)); if (text) node.textContent = text; svg.append(node); };
    [0, .5, 1].forEach(v => { const y = 145 - v * 125; draw('line', { x1: 40, x2: 710, y1: y, y2: y, stroke: '#dddddd' }); draw('text', { x: 0, y: y + 4, 'font-size': 11 }, percent(v)); });
    ['T1', 'T2'].forEach((turbine, i) => {
      const points = hours.map((hour, index) => {
        const row = rows.find(r => r.time === hour && r.turbine === turbine);
        return row && finite(row.p50) ? (40 + index * 670 / Math.max(1, hours.length - 1)) + ',' + (145 - row.p50 * 125) : null;
      }).filter(Boolean).join(' ');
      draw('polyline', { points, fill: 'none', stroke: i ? '#666666' : '#23684c', 'stroke-width': 2, 'stroke-dasharray': i ? '6 3' : 'none' });
    });
    draw('text', { x: 40, y: 170, 'font-size': 11 }, local(hours[0]));
    draw('text', { x: 710, y: 170, 'font-size': 11, 'text-anchor': 'end' }, local(hours.at(-1)) + ' UTC+5');
    return svg;
  }
  function printReport(report) {
    document.getElementById('wind-print-report')?.remove();
    const host = element('article', undefined, 'wind-print-report'); host.id = 'wind-print-report';
    host.append(element('p', 'WIND / ОТЧЁТ СОБСТВЕННИКУ', 'report-eyebrow'), element('h1', report.payload.site?.name || 'Прогноз выработки ВЭС'), element('p', report.title));
    const metrics = (report.payload.metrics || []).filter(r => r.model === 'наше решение');
    const kpis = element('div', undefined, 'report-kpis');
    [[percent(metrics.find(r => r.lead_h === 24)?.nmae), 'Ошибка прогноза 1–24 ч'], [percent(metrics.find(r => r.lead_h === 48)?.nmae), 'Ошибка прогноза 25–48 ч'], [percent(report.payload.coverage), 'Факт внутри P10–P90']].forEach(([value, label]) => {
      const block = element('div'); block.append(element('strong', value), element('span', label)); kpis.append(block);
    });
    host.append(kpis, element('p', 'Точность — на отложенном периоде: ' + local(report.payload.validation_period?.start) + ' — ' + local(report.payload.validation_period?.end) + ' (UTC+5), без часов ограничения выдачи. Факта февраля нет.', 'report-note'));
    if (!report.all) {
      host.append(element('h2', 'Прогноз по часам'), chart(report.rows), element('p', 'P50: Т1 — зелёная линия, Т2 — серая пунктирная. Мощность в долях номинала.', 'report-note'));
      const explanation = report.journal.filter(r => r.step === 'explain').at(-1);
      if (explanation) host.append(element('p', 'Пояснение агента (время UTC): ' + explanation.text));
    }
    host.append(element('h2', 'Почасовая выработка'), element('p', 'P10–P90 — диапазон для отдельной турбины и часа. Значения — % номинала; ветер — прогноз на высоте 100 м. Все даты таблицы — UTC+5.', 'report-note'));
    host.append(table(['Выпуск', 'Час', 'Турбина', 'До часа', 'P10', 'P50', 'P90', 'Ветер, м/с'], report.rows.map(r => [local(r.issue_time), local(r.time), r.turbine, number(r.effective_lead_h ?? ((utc(r.time) - utc(r.issue_time)) / 3600000), 0), percent(r.p10), percent(r.p50), percent(r.p90), number(r.wind_speed_100m)])));
    host.append(element('h2', 'Источники и ограничения'), table(['Параметр', 'Значение'], notes(report)));
    document.body.append(host); document.body.classList.add('printing-wind-report');
    const clean = () => { document.body.classList.remove('printing-wind-report'); host.remove(); };
    window.addEventListener('afterprint', clean, { once: true });
    window.print();
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('#exportExcel, #exportPdf');
    if (!button) return;
    try {
      const report = selection();
      if (button.id === 'exportExcel') excel(report); else printReport(report);
      document.getElementById('report-export-status')?.remove();
    } catch (error) {
      let status = document.getElementById('report-export-status');
      if (!status) { status = element('p'); status.id = 'report-export-status'; status.setAttribute('role', 'status'); button.parentNode.append(status); }
      status.textContent = error.message;
    }
  });
}());
