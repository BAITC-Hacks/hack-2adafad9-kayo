(function () {
  'use strict';
  const assets = new URL('vendor/pdf/', document.currentScript.src);
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const safe = value => String(value ?? '').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '');
  const num = (value, digits = 1) => finite(value) ? value.toLocaleString('ru-RU', {maximumFractionDigits: digits}).replace(/\u00a0/g, ' ') : '—';
  const pct = value => finite(value) ? num(value * 100) + ' %' : '—';
  const utc = value => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z');
  const local = value => {
    if (!value) return '—';
    const stamp = utc(value);
    return Number.isFinite(stamp.getTime()) ? new Date(stamp.getTime() + 5 * 3600000).toISOString().slice(0, 16).replace('T', ' ') : '—';
  };
  let ready;
  function script(name) {
    return new Promise((resolve, reject) => {
      const tag = document.createElement('script');
      tag.src = new URL(name, assets).href;
      tag.onload = resolve;
      tag.onerror = () => reject(new Error('Не удалось загрузить локальный компонент PDF: ' + name));
      document.head.append(tag);
    });
  }
  function dependencies() {
    if (!ready) ready = (async () => {
      if (!window.jspdf) await script('jspdf.umd.min.js');
      if (!window.jspdf.jsPDF.API.autoTable) await script('jspdf.plugin.autotable.min.js');
      if (!window.WindPDFFont) await script('dejavu-font.js');
    })().catch(error => { ready = null; throw error; });
    return ready;
  }

  async function createBlob(report) {
    if (!report.rows?.length) throw new Error('В выбранном периоде нет прогноза для PDF.');
    await dependencies();
    const doc = new window.jspdf.jsPDF({unit: 'mm', format: 'a4', compress: true, putOnlyUsedFonts: true});
    doc.addFileToVFS('DejaVuSans.ttf', window.WindPDFFont);
    doc.addFont('DejaVuSans.ttf', 'WindSans', 'normal');
    doc.setFont('WindSans', 'normal');
    doc.setProperties({title: 'Прогноз выработки ВЭС', subject: safe(report.title), author: 'Wind Forecast'});
    const left = 14, width = 182, bottom = 279;
    let y = 18;
    const payload = report.payload || {};
    function room(height) {
      if (y + height > bottom) { doc.addPage(); y = 18; }
    }
    function paragraph(text, size = 9, color = 70) {
      doc.setFont('WindSans', 'normal'); doc.setFontSize(size); doc.setTextColor(color);
      const lines = doc.splitTextToSize(safe(text), width), step = size * 0.43;
      for (const line of lines) { room(step); doc.text(line, left, y); y += step; }
      y += 3;
    }
    function heading(text) { room(16); y += 3; paragraph(text, 13, 35); }
    function table(headers, rows, options = {}) {
      room(18);
      doc.autoTable({startY: y, head: [headers], body: rows, margin: {left, right: left, top: 17, bottom: 18},
        styles: {font: 'WindSans', fontStyle: 'normal', fontSize: 7, cellPadding: 1.7, textColor: 35, overflow: 'linebreak'},
        headStyles: {font: 'WindSans', fontStyle: 'normal', fillColor: [42, 99, 77], textColor: 255},
        alternateRowStyles: {fillColor: [247, 248, 247]}, theme: 'striped',
        showHead: 'everyPage', rowPageBreak: 'avoid', ...options});
      y = doc.lastAutoTable.finalY + 6;
    }

    paragraph('WIND / ОТЧЁТ СОБСТВЕННИКУ', 9, 65);
    paragraph(payload.site?.name || 'Прогноз выработки ВЭС', 20, 30);
    paragraph(report.title || (report.all ? 'Весь период поставки' : 'Выпуск ' + local(report.issued) + ' (UTC+5)'), 11);
    paragraph('Отчёт сформирован: ' + local(new Date().toISOString()) + ' (UTC+5). Данные прогона: ' + (payload.generated_at || 'не указано') + '.', 8);
    const ours = (payload.metrics || []).filter(row => row.model === 'наше решение');
    table(['Ошибка 1–24 ч, nMAE', 'Ошибка 25–48 ч, nMAE', 'Покрытие P10–P90'], [[
      pct(ours.find(row => row.lead_h === 24)?.nmae), pct(ours.find(row => row.lead_h === 48)?.nmae), pct(payload.coverage)
    ]], {styles: {font: 'WindSans', fontStyle: 'normal', fontSize: 12, cellPadding: 3}, headStyles: {fontSize: 8, fillColor: [42, 99, 77], fontStyle: 'normal'}});
    paragraph('nMAE — средняя абсолютная ошибка в долях номинала, без выявленных простоев. Покрытие — доля всех часов с известным фактом внутри диапазона. Валидация: ' + local(payload.validation_period?.start) + ' — ' + local(payload.validation_period?.end) + ' (UTC+5). Факта февраля нет.', 8);

    if (!report.all) {
      room(78); heading('Прогноз P50 по часам');
      const hours = [...new Set(report.rows.map(row => row.time))].sort();
      const byKey = new Map(report.rows.map(row => [row.turbine + '|' + row.time, row]));
      const top = y + 3, plotLeft = 26, plotWidth = 169, plotHeight = 38;
      doc.setFontSize(7); doc.setTextColor(80);
      [0, 0.5, 1].forEach(value => {
        const lineY = top + plotHeight * (1 - value);
        doc.setDrawColor(220); doc.setLineWidth(0.15); doc.line(plotLeft, lineY, plotLeft + plotWidth, lineY);
        doc.text(pct(value), left, lineY + 1);
      });
      ['T1', 'T2'].forEach((turbine, index) => {
        doc.setDrawColor(...(index ? [105, 105, 105] : [42, 99, 77]));
        doc.setLineWidth(0.55); doc.setLineDashPattern(index ? [2, 1.2] : [], 0);
        let previous;
        hours.forEach((hour, position) => {
          const row = byKey.get(turbine + '|' + hour);
          if (!finite(row?.p50)) { previous = null; return; }
          const point = [plotLeft + plotWidth * position / Math.max(1, hours.length - 1), top + plotHeight * (1 - row.p50)];
          if (previous) doc.line(previous[0], previous[1], point[0], point[1]);
          previous = point;
        });
      });
      doc.setLineDashPattern([], 0); doc.setTextColor(70);
      doc.text(local(hours[0]), plotLeft, top + plotHeight + 5);
      doc.text(local(hours.at(-1)), plotLeft + plotWidth, top + plotHeight + 5, {align: 'right'});
      y = top + plotHeight + 11;
      paragraph('T1 — зелёная сплошная; T2 — серая пунктирная. Мощность — % номинала; время — UTC+5.', 8);
      const explanation = (report.journal || []).filter(entry => entry.step === 'explain').at(-1);
      if (explanation) paragraph('Пояснение агента (время в тексте — UTC): ' + explanation.text, 8);
    }

    const money = report.economics;
    if (money && [money.capacityMw, money.penaltyPerMwh, money.baselineCost, money.agentCost, money.savings, money.energyMwh].every(finite)) {
      heading('Сценарная стоимость ошибки');
      paragraph('Весь период поставки: ' + num(money.hours, 0) + ' часов, ' + num(money.turbines, 0) + ' турбины. Выбор выпуска не меняет этот расчёт.', 8);
      table(['«Как вчера», ежедневный факт', 'Агент, без нового факта', 'Разница условных затрат'], [[
        num(money.baselineCost, 0) + ' ₸', num(money.agentCost, 0) + ' ₸', num(Math.abs(money.savings), 0) + ' ₸ ' + (money.savings >= 0 ? 'меньше' : 'больше')
      ]]);
      paragraph('ДОПУЩЕНИЯ: ' + num(money.capacityMw, 2) + ' МВт на турбину; условная цена ошибки ' + num(money.penaltyPerMwh, 0) + ' ₸/МВт·ч. Затраты = nMAE 1–24 ч × число турбин × номинал × часы × цена ошибки. Ошибка валидации без простоев перенесена на масштаб месяца; ошибки турбин суммируются без взаимозачёта. Это не фактическая выручка, прибыль или достигнутая экономия.', 8);
      paragraph('Прогноз выработки по P50: ' + num(money.energyMwh, 1) + ' МВт·ч. Сумма почасовых P50 обеих турбин только для периода 1–24 ч, без повторного учёта 25–48 ч.', 8);
    }

    heading('Точность на проверочном периоде');
    table(['Модель', 'Период, ч', 'nMAE', 'nRMSE', 'Смещение', 'Скилл'], (payload.metrics || []).map(row => [
      safe(row.model), row.lead_h === 24 ? '1–24' : '25–48', pct(row.nmae), pct(row.nrmse), pct(row.bias), pct(row.skill)
    ]), {columnStyles: {0: {cellWidth: 64}}});
    paragraph('Метрики — без выявленных простоев. Скилл сравнивает ошибку с персистентностью при ежедневном факте; агент в режиме поставки не получает нового факта с начала месяца.', 8);

    heading('Источники и ограничения');
    paragraph('Open-Meteo Previous Runs API; ICON / GFS / ECMWF. Архив day2/day3, ежедневный выпуск в 23:00 UTC. Номинальное reference time — целевой час минус возраст прогноза, не точное время запуска модели. ' + (payload.forecast_protocol?.availability_assumption || 'ДОПУЩЕНИЕ: задержка публикации погоды не превышала 24 часа.'), 8);
    paragraph('P10–P90 — целевой 80-процентный диапазон отдельной турбины и часа; гарантии покрытия нет. Фактическая выработка февраля отсутствует. Прогноз и метрики взяты из текущего прогона web/data.json. Местное время = UTC+5.', 8);

    doc.addPage(); y = 18;
    heading('Полный почасовой прогноз');
    paragraph('Все даты таблицы — UTC+5. До часа — фактический срок от выпуска. P10/P50/P90 — % номинала. Ветер — прогноз на высоте 100 м. Строк: ' + num(report.rows.length, 0) + '.', 8);
    table(['Выпуск UTC+5', 'Час UTC+5', 'Турбина', 'До часа, ч', 'P10', 'P50', 'P90', 'Ветер, м/с'], report.rows.map(row => [
      local(row.issue_time), local(row.time), safe(row.turbine), num(row.effective_lead_h, 0), pct(row.p10), pct(row.p50), pct(row.p90), num(row.wind_speed_100m)
    ]), {styles: {font: 'WindSans', fontStyle: 'normal', fontSize: 6.7, cellPadding: 1.65, overflow: 'linebreak'},
      columnStyles: {0: {cellWidth: 34}, 1: {cellWidth: 34}, 2: {cellWidth: 15}, 3: {cellWidth: 19}, 4: {cellWidth: 18}, 5: {cellWidth: 18}, 6: {cellWidth: 18}, 7: {cellWidth: 26}}});
    const count = doc.getNumberOfPages();
    for (let page = 1; page <= count; page++) {
      doc.setPage(page); doc.setFont('WindSans', 'normal'); doc.setFontSize(7); doc.setTextColor(100);
      doc.text('ВЭС · ' + (report.all ? 'весь период' : 'выбранный выпуск') + ' · ' + page + ' / ' + count, left, 289);
      doc.text('Время таблицы: UTC+5', 196, 289, {align: 'right'});
    }
    return doc.output('blob');
  }

  async function download(report) {
    const blob = await createBlob(report);
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url;
    link.download = 'wind-report-' + (report.all ? 'all' : String(report.issued || 'forecast').replace(/[^0-9A-Za-z_-]/g, '-')) + '.pdf';
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    return blob;
  }
  window.WindPDF = {download, createBlob};
}());
