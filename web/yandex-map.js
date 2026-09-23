(() => {
  'use strict';
  const configUrl = new URL('map-config.json', document.currentScript.src).href;
  // Координаты из ссылок организаторов в условии кейса, порядок JSAPI — долгота, широта.
  // T1: maps.app.goo.gl/iN6svMt69D5qRpFU9; T2: maps.app.goo.gl/8UQMwsYavY6nLvFY8.
  const turbines = [
    {name:'T1',coordinates:[78.535604,43.645150]},
    {name:'T2',coordinates:[78.538828,43.643198]}
  ];
  let apiPromise;
  function loadApi() {
    if (apiPromise) return apiPromise;
    apiPromise = (async () => {
      const config = await fetch(configUrl, {cache:'no-store'});
      if (!config.ok) throw new Error('not-configured');
      const settings = await config.json();
      if (typeof settings.apiKey !== 'string' || !settings.apiKey.trim()) throw new Error('not-configured');
      if (!window.ymaps3) await new Promise((resolve,reject) => {
        const script = document.createElement('script');
        const timeout = setTimeout(() => { script.remove(); reject(new Error('unavailable')); },12000);
        script.async = true;
        script.referrerPolicy = 'strict-origin-when-cross-origin';
        script.src = 'https://api-maps.yandex.ru/v3/?apikey=' + encodeURIComponent(settings.apiKey) + '&lang=ru_RU';
        script.onload = () => { clearTimeout(timeout); resolve(); };
        script.onerror = () => { clearTimeout(timeout); script.remove(); reject(new Error('unavailable')); };
        document.head.append(script);
      });
      if (!window.ymaps3) throw new Error('unavailable');
      await Promise.race([window.ymaps3.ready,new Promise((_,reject)=>setTimeout(()=>reject(new Error('unavailable')),12000))]);
      return window.ymaps3;
    })().catch(error => { apiPromise=null; throw new Error(error.message==='not-configured'?'not-configured':'unavailable'); });
    return apiPromise;
  }
  async function mount(host) {
    const api = await loadApi();
    const initial = {center:[78.537216,43.64465],zoom:16.7};
    const map = new api.YMap(host, {location:initial,theme:'light',behaviors:['drag','pinchZoom','dblClick']}, [
      new api.YMapDefaultSchemeLayer({}),new api.YMapDefaultFeaturesLayer({})
    ]);
    const labels = turbines.map(({name,coordinates}) => {
      const marker = document.createElement('div');marker.className='forecast-map-marker';
      const heading=document.createElement('span');heading.className='forecast-map-marker-name';heading.textContent=name;
      const value=document.createElement('strong');value.textContent='—';
      const interval=document.createElement('small');interval.textContent='Нет прогноза';
      marker.append(heading,value,interval);
      marker.title='Координаты из условия: '+coordinates[1].toFixed(6)+', '+coordinates[0].toFixed(6);
      map.addChild(new api.YMapMarker({coordinates},marker));
      return {name,value,interval,marker};
    });
    const pct=value=>(value*100).toFixed(1).replace('.',',')+'%';
    return {
      update(rows,time) { labels.forEach(label=>{const row=rows.find(row=>row&&row.turbine===label.name);label.value.textContent=row?pct(row.p50):'—';label.interval.textContent=row?'P10–P90: '+pct(row.p10)+'–'+pct(row.p90):'Нет прогноза';label.marker.setAttribute('aria-label',label.name+', '+time+', прогноз '+label.value.textContent+', '+label.interval.textContent);}); },
      reset() { map.setLocation({...initial,duration:window.matchMedia('(prefers-reduced-motion: reduce)').matches?0:700}); },
      zoom(delta) { map.setLocation({zoom:Math.max(5,Math.min(20,map.zoom+delta)),duration:window.matchMedia('(prefers-reduced-motion: reduce)').matches?0:250}); },
      destroy() { map.destroy(); }
    };
  }
  window.YandexForecastMap={mount};
})();
