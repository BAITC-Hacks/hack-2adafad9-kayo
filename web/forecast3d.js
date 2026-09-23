(() => {
  'use strict';
  const accent = '#146e5a';
  const utc = value => Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : value + 'Z');
  const local = (stamp, hour = true) => new Intl.DateTimeFormat('ru-RU', {
    timeZone:'Asia/Almaty', day:'2-digit', month:'2-digit', ...(hour ? {hour:'2-digit',minute:'2-digit'} : {})
  }).format(new Date(stamp));
  const percent = value => (value * 100).toFixed(1).replace('.', ',') + '%';
  const moduleUrl = new URL('vendor/three.module.js', document.currentScript.src).href;
  function mount(host, forecast) {
    const root = document.createElement('div'); root.className='forecast3d'; host.append(root);
    const el = (tag, cls, text, parent=root) => {
      const node=document.createElement(tag); if(cls)node.className=cls;
      if(text!==undefined)node.textContent=text;parent.append(node);return node;
    };
    const releases=new Map();
    for(const row of forecast || []) {
      if(!['T1','T2'].includes(row.turbine)||!Number.isFinite(utc(row.time))||!Number.isFinite(utc(row.issue_time))||![row.p10,row.p50,row.p90].every(Number.isFinite))continue;
      if(!releases.has(row.issue_time))releases.set(row.issue_time,[]);
      releases.get(row.issue_time).push(row);
    }
    const ordered=[...releases].sort((a,b)=>utc(b[0])-utc(a[0]));
    // Последний выпуск может обрываться на границе месяца.
    const selected=ordered.find(([,rows])=>['T1','T2'].every(turbine=>new Set(rows.filter(row=>row.turbine===turbine).map(row=>row.time)).size>=48))||ordered[0];
    if(!selected){el('p','forecast3d-note','Нет данных для сцены прогноза.');return {destroy:()=>root.remove(),resize(){}};}
    const times=[...new Set(selected[1].map(row=>row.time))].sort((a,b)=>utc(a)-utc(b)).slice(0,48);
    const series=['T1','T2'].map(turbine=>{const hours=new Map(selected[1].filter(row=>row.turbine===turbine).map(row=>[row.time,row]));return {turbine,rows:times.map(time=>hours.get(time))};});
    const stage=el('div','forecast3d-stage'),canvas=el('canvas','forecast3d-scene',undefined,stage);
    const mapHost=el('div','forecast3d-map',undefined,stage);mapHost.setAttribute('aria-hidden','true');mapHost.inert=true;
    const head=el('div','forecast3d-head',undefined,stage),heading=el('div','',undefined,head);
    el('h3','','Прогноз на площадке · '+times.length+' часов',heading);
    el('p','forecast3d-meta','Выпуск '+local(utc(selected[0]))+' · UTC+5',heading);
    const controls=el('div','forecast3d-controls',undefined,head),views=el('div','forecast3d-views',undefined,controls);views.setAttribute('role','group');views.setAttribute('aria-label','Вид площадки');
    const perspective=el('button','forecast3d-volume','3D-сцена',views),overhead=el('button','forecast3d-map-button','Карта',views);perspective.type=overhead.type='button';perspective.setAttribute('aria-pressed','true');overhead.setAttribute('aria-pressed','false');
    const reset=el('button','forecast3d-reset','Сбросить вид',controls);reset.type='button';reset.title='Перетаскивайте сцену, чтобы повернуть камеру. Сброс возвращает исходный ракурс выбранного вида.';
    const mapStatus=el('div','forecast3d-map-status','',stage);mapStatus.hidden=true;mapStatus.setAttribute('role','status');
    const mapZoom=el('div','forecast3d-map-zoom',undefined,stage);mapZoom.hidden=true;
    const zoomIn=el('button','','+',mapZoom),zoomOut=el('button','','−',mapZoom);zoomIn.type=zoomOut.type='button';zoomIn.setAttribute('aria-label','Приблизить карту');zoomOut.setAttribute('aria-label','Отдалить карту');
    canvas.setAttribute('role','img');canvas.setAttribute('aria-label','Условная сцена Шелекского коридора: две ветротурбины. Прогноз мощности, диапазон P10–P90 и график каждой турбины показаны на панелях.');
    const svgEl=(tag,attrs,parent)=>{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);parent.append(node);return node;};
    const panels=series.map(({turbine,rows})=>{
      const panel=el('div','forecast3d-panel',undefined,stage);panel.dataset.turbine=turbine;el('span','forecast3d-panel-name',turbine+' · доля мощности',panel);
      const median=el('strong','forecast3d-value','',panel),band=el('span','forecast3d-band-label','',panel);
      const spark=svgEl('svg',{viewBox:'0 0 200 58',class:'forecast3d-spark','aria-hidden':'true'},panel);
      const x=i=>times.length>1?i/(times.length-1)*200:100;
      // Пропущенные часы остаются разрывами.
      let run=[];
      const flush=()=>{if(!run.length)return;
        const bounds=run.map(i=>`${x(i)},${54-rows[i].p90*50}`).concat([...run].reverse().map(i=>`${x(i)},${54-rows[i].p10*50}`));
        svgEl('polygon',{points:bounds.join(' '),fill:'rgba(20,110,90,.14)'},spark);
        svgEl('polyline',{points:run.map(i=>`${x(i)},${54-rows[i].p50*50}`).join(' '),fill:'none',stroke:accent,'stroke-width':2},spark);run=[];};
      rows.forEach((row,i)=>{if(!row){flush();return;}if(i&&utc(times[i])-utc(times[i-1])!==3600000)flush();run.push(i);});flush();
      const marker=svgEl('line',{x1:0,x2:0,y1:0,y2:58,stroke:accent,'stroke-width':1,'stroke-dasharray':'2 3'},spark),dot=svgEl('circle',{cx:0,cy:54,r:3,fill:accent},spark);
      const caption=el('div','forecast3d-spark-caption',undefined,panel);el('span','','P50 + P10–P90',caption);el('span','',times.length+' ч',caption);
      return {panel,median,band,marker,dot};
    });
    const timeline=el('div','forecast3d-timeline',undefined,stage),timeHead=el('div','forecast3d-time-head',undefined,timeline);
    const play=el('button','forecast3d-play','Пуск',timeHead);play.type='button';play.setAttribute('aria-pressed','false');play.setAttribute('aria-label','Воспроизвести прогноз по часам');
    const current=el('div','forecast3d-current',undefined,timeHead),hour=el('span','forecast3d-time','',current);el('span','forecast3d-time-zone','UTC+5',current);
    const windReadout=el('div','forecast3d-wind',undefined,timeHead);el('span','forecast3d-wind-label','Ветер · 100 м',windReadout);const windValue=el('strong','forecast3d-wind-value','—',windReadout);
    const slider=el('input','forecast3d-range',undefined,timeline);slider.type='range';slider.min=0;slider.max=times.length-1;slider.value=0;slider.setAttribute('aria-label','Час прогноза');
    const limits=el('div','forecast3d-limits',undefined,timeline);
    for(const index of [...new Set([0,12,24,36,times.length-1].filter(index=>index<times.length))]){const tick=el('span','forecast3d-tick','',limits);tick.style.left=(index/Math.max(1,times.length-1)*100)+'%';el('span','',local(utc(times[index]),false),tick);el('span','',local(utc(times[index])).slice(-5),tick);}
    timeline.title='Условная площадка. Вращение лопастей иллюстрирует прогноз ветра, а не реальные обороты турбин. Направление ветра указано по правилу «откуда дует».';
    const ctx=canvas.getContext('2d');
    let width=0,height=0,active=0,yaw=0,elevation=0,drag=null,frame=0,playing=false,lastStep=0,visible=true,destroyed=false,scene3d=null,rotorPhase=0,lastRender=0,rotorSpeed=.4;
    let mapController=null,mapPending=null,mapRequested=false,mapActive=false,resetting=false;
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
    const windAt=index=>{const readings=series.map(({rows})=>rows[index]?.wind_speed_100m).filter(value=>Number.isFinite(value)&&value>=0);return readings.length?readings.reduce((sum,value)=>sum+value,0)/readings.length:null;};
    const directionAt=index=>{const bearings=series.map(({rows})=>rows[index]?.wind_direction_100m).filter(Number.isFinite);if(!bearings.length)return null;return Math.atan2(bearings.reduce((sum,angle)=>sum+Math.sin(angle*Math.PI/180),0),bearings.reduce((sum,angle)=>sum+Math.cos(angle*Math.PI/180),0));};
    let flowAngle=directionAt(0)===null?Math.atan2(1,.2):-directionAt(0);
    const streamCount=22,streamSegments=18;
    const streams=Array.from({length:streamCount},(_,index)=>({index,age:0,life:8+index%5,x:0,z:0,y:7.5+(index*1.731)%9,fade:0}));
    function resetStream(stream,initial=false){const dx=Math.sin(flowAngle),dz=Math.cos(flowAngle),cross=((stream.index*5.371)%24)-12;stream.age=initial?(stream.index*.713)%stream.life:0;stream.x=-dx*17-dz*cross+dx*stream.age*4;stream.z=-dz*17+dx*cross+dz*stream.age*4;}
    streams.forEach(stream=>resetStream(stream,true));
    function mapState(enabled){mapActive=enabled;root.dataset.view=enabled?'map':'perspective';perspective.setAttribute('aria-pressed',String(!enabled));overhead.setAttribute('aria-pressed',String(enabled));mapHost.setAttribute('aria-hidden',String(!enabled));mapHost.inert=!enabled;mapZoom.hidden=!enabled;schedule();}
    async function chooseView(showMap){mapRequested=showMap;if(!showMap){mapState(false);mapStatus.hidden=true;return;}mapStatus.hidden=false;mapStatus.textContent='Загружаем карту…';overhead.setAttribute('aria-busy','true');
      try{if(!mapController){if(!window.YandexForecastMap)throw new Error('unavailable');if(!mapPending)mapPending=window.YandexForecastMap.mount(mapHost);mapController=await mapPending;if(destroyed){mapController.destroy();return;}}
        mapController.update(series.map(({rows})=>rows[active]),local(utc(times[active])));if(mapRequested){mapState(true);mapStatus.textContent='Координаты турбин из условия кейса';}
      }catch(error){mapPending=null;mapState(false);mapStatus.hidden=!mapRequested;mapStatus.textContent=error.message==='not-configured'?'Карта ещё не подключена. Прогноз доступен в 3D.':'Карта пока недоступна. Нажмите «Карта» позже, чтобы повторить. Прогноз в 3D работает.';}
      finally{overhead.removeAttribute('aria-busy');}}
    perspective.addEventListener('click',()=>chooseView(false));overhead.addEventListener('click',()=>chooseView(true));
    zoomIn.addEventListener('click',()=>mapController?.zoom(1));zoomOut.addEventListener('click',()=>mapController?.zoom(-1));
    function update(){hour.textContent=local(utc(times[active]));slider.value=active;slider.style.setProperty('--position',(active/Math.max(1,times.length-1)*100)+'%');slider.setAttribute('aria-valuetext',hour.textContent);
      series.forEach(({rows},i)=>{const row=rows[active],panel=panels[i],x=times.length>1?active/(times.length-1)*200:100;
        panel.median.textContent=row?percent(row.p50):'—';panel.band.textContent=row?'P10–P90: '+percent(row.p10)+'–'+percent(row.p90):'Нет прогноза';
        panel.marker.setAttribute('x1',x);panel.marker.setAttribute('x2',x);panel.dot.setAttribute('cx',x);panel.dot.setAttribute('cy',row?54-row.p50*50:54);panel.dot.style.display=row?'':'none';});
      const wind=windAt(active),bearing=directionAt(active),compass=['С','СВ','В','ЮВ','Ю','ЮЗ','З','СЗ'];
      const direction=bearing===null?'':' '+compass[Math.round(((bearing*180/Math.PI+360)%360)/45)%8];
      windValue.textContent=wind===null?'Нет данных':wind.toFixed(1).replace('.',',')+' м/с';
      windReadout.title=wind===null?'Прогноз ветра недоступен. Вращение и потоки условные.':'Прогноз ветра на 100 м: '+wind.toFixed(1).replace('.',',')+' м/с'+direction+'. Вращение и потоки показаны схематично.';
      windReadout.setAttribute('aria-label',windReadout.title);if(mapController)mapController.update(series.map(({rows})=>rows[active]),hour.textContent);}
    function render(stamp){frame=0;if(destroyed||!visible)return;
      const wind=windAt(active),elapsed=lastRender?Math.min((stamp-lastRender)/1000,.1):0;lastRender=stamp;
      if(resetting){const smoothing=reduced.matches?1:1-Math.exp(-elapsed*7);yaw+=(0-yaw)*smoothing;elevation+=(0-elevation)*smoothing;if(Math.abs(yaw)<.001&&Math.abs(elevation)<.01){yaw=0;elevation=0;resetting=false;}}
      const targetSpeed=wind===null?.4:Math.min(wind*.055,1.4);rotorSpeed+=(targetSpeed-rotorSpeed)*(1-Math.exp(-elapsed*2.8));
      if(!reduced.matches)rotorPhase+=elapsed*rotorSpeed;
      const bearing=directionAt(active),targetAngle=bearing===null?Math.atan2(1,.2):-bearing,turn=Math.atan2(Math.sin(targetAngle-flowAngle),Math.cos(targetAngle-flowAngle));flowAngle=reduced.matches?targetAngle:flowAngle+turn*(1-Math.exp(-elapsed*1.2));
      for(const stream of streams){if(!reduced.matches){stream.age+=elapsed*rotorSpeed/.4;stream.x+=Math.sin(flowAngle)*elapsed*rotorSpeed*10;stream.z+=Math.cos(flowAngle)*elapsed*rotorSpeed*10;if(stream.age>=stream.life)resetStream(stream);}stream.fade=Math.pow(Math.max(0,Math.sin(Math.PI*stream.age/stream.life)),1.4)*Math.min(1,rotorSpeed/.2);}
      if(playing&&stamp-lastStep>800){active=(active+1)%times.length;lastStep=stamp;update();}
      if(mapActive){if(ctx)ctx.clearRect(0,0,width,height);}
      else if(scene3d){scene3d.render(stamp,yaw);}
      else if(ctx&&width){
        const mobile=width<600,horizon=height*(mobile?.43:.38),ground=height*(mobile?.77:.79);
        const sky=ctx.createLinearGradient(0,0,0,height);sky.addColorStop(0,'#76aaca');sky.addColorStop(.48,'#dde5df');sky.addColorStop(1,'#c4b38d');ctx.fillStyle=sky;ctx.fillRect(0,0,width,height);
        for(let cloud=0;cloud<5;cloud++){const x=width*(.12+cloud*.19),y=horizon*(.29+(cloud%2)*.28),radius=width*(.09+(cloud%3)*.025);ctx.save();ctx.translate(x,y);ctx.scale(1,.28);const haze=ctx.createRadialGradient(0,0,0,0,0,radius);haze.addColorStop(0,'rgba(255,255,251,.25)');haze.addColorStop(.5,'rgba(255,255,251,.12)');haze.addColorStop(1,'rgba(255,255,251,0)');ctx.fillStyle=haze;ctx.fillRect(-radius,-radius,radius*2,radius*2);ctx.restore();}
        const mountain=(baseline,amp,fill,phase)=>{const ridges=[];ctx.beginPath();ctx.moveTo(0,baseline);for(let x=0;x<=width+24;x+=24){const n=x/width,peak=Math.sin(n*12+phase)*.32+Math.sin(n*31+phase)*.15+Math.sin(n*5+phase)*.5+Math.sin(n*87+phase)*.07;const y=baseline-amp*(.55+peak);ctx.lineTo(x,y);ridges.push([x,y]);}ctx.lineTo(width,height);ctx.lineTo(0,height);ctx.closePath();ctx.fillStyle=fill;ctx.fill();ridges.forEach(([x,y],i)=>{if(i%3)return;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x+amp*.65,baseline+8);ctx.lineTo(x-amp*.45,baseline+8);ctx.closePath();ctx.fillStyle='rgba(242,245,235,.1)';ctx.fill();});};
        mountain(horizon+26,55,'#c9d4cf',.5+yaw*.3);mountain(horizon+48,34,'#b8c6bb',2+yaw*.2);
        const soil=ctx.createLinearGradient(0,horizon+35,0,height);soil.addColorStop(0,'#cfc4a7');soil.addColorStop(1,'#b5a075');ctx.fillStyle=soil;ctx.beginPath();ctx.moveTo(0,horizon+44);ctx.quadraticCurveTo(width*.5,horizon+24,width,horizon+48);ctx.lineTo(width,height);ctx.lineTo(0,height);ctx.fill();
        const project=([x,y,z])=>{const rx=x*Math.cos(yaw)+z*Math.sin(yaw),rz=-x*Math.sin(yaw)+z*Math.cos(yaw),scale=(mobile?width*.15:Math.min(width*.075,86))/(1+rz*.075);return[width*.5+rx*scale,ground-y*scale-rz*scale*.2,scale];};
        const polygon=(points,fill,stroke)=>{ctx.beginPath();points.forEach((point,i)=>{const p=project(point);if(i)ctx.lineTo(p[0],p[1]);else ctx.moveTo(p[0],p[1]);});ctx.closePath();if(fill){ctx.fillStyle=fill;ctx.fill();}if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1;ctx.stroke();}};
        // Детерминированная фактура степи не мерцает между кадрами.
        for(let i=0;i<110;i++){const x=((i*73)%997)/997*width,depth=((i*47)%101)/101,y=horizon+52+depth*(height-horizon-52);ctx.strokeStyle=depth>.5?'rgba(91,107,81,.13)':'rgba(91,107,81,.07)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x+2+depth*8,y-depth*2);ctx.stroke();}
        for(const stream of streams){for(let segment=0;segment<streamSegments;segment++){const a=segment/streamSegments,b=(segment+1)/streamSegments;const point=t=>project([(stream.x+(t-1)*4*Math.sin(flowAngle))*.27,stream.y*(mobile?.31:.245),(stream.z+(t-1)*4*Math.cos(flowAngle))*.2]);const p=point(a),q=point(b);ctx.beginPath();ctx.moveTo(p[0],p[1]);ctx.lineTo(q[0],q[1]);ctx.strokeStyle='rgba(255,253,239,'+(.6*b*b*stream.fade)+')';ctx.lineWidth=1.4;ctx.stroke();}}
        const turbines=[{x:mobile?-1.75:-2,z:1.6,index:0},{x:mobile?1.8:2,z:3.4,index:1}],hubs=[];
        [...turbines].sort((a,b)=>b.z-a.z).forEach(({x,z,index})=>{
          const tower=mobile?4.05:3.15,radius=mobile?1.21:1.04,base=project([x,0,z]);
          polygon([[x-.13,0,z],[x+.13,0,z],[x+2.2,0,z+1.8],[x+2.1,0,z+2]],'rgba(49,67,44,.12)');
          ctx.save();ctx.translate(base[0],base[1]);ctx.scale(1,.24);ctx.beginPath();ctx.ellipse(0,0,base[2]*.48,base[2]*.32,0,0,Math.PI*2);ctx.fillStyle='#b2b9a6';ctx.fill();ctx.restore();
          polygon([[x-.3,.04,z-.28],[x+.3,.04,z-.28],[x+.3,.04,z+.28],[x-.3,.04,z+.28]],'#dce0d4');
          polygon([[x-.3,0,z-.28],[x+.3,0,z-.28],[x+.3,.04,z-.28],[x-.3,.04,z-.28]],'#a7b4a9');
          // Цилиндрическая башня: освещённая сторона и плавный уход в тень.
          const towerFoot=project([x,0,z]),towerTop=project([x,tower,z]);
          const towerLight=ctx.createLinearGradient(towerFoot[0]-base[2]*.15,0,towerFoot[0]+base[2]*.15,0);
          towerLight.addColorStop(0,'#a6b5ac');towerLight.addColorStop(.28,'#fdfdf7');towerLight.addColorStop(.6,'#e9eee6');towerLight.addColorStop(1,'#8b9f94');
          ctx.beginPath();ctx.moveTo(towerFoot[0]-base[2]*.145,towerFoot[1]);ctx.lineTo(towerFoot[0]+base[2]*.145,towerFoot[1]);ctx.lineTo(towerTop[0]+towerTop[2]*.07,towerTop[1]);ctx.lineTo(towerTop[0]-towerTop[2]*.07,towerTop[1]);ctx.closePath();ctx.fillStyle=towerLight;ctx.fill();
          polygon([[x-.045,.02,z-.15],[x+.045,.02,z-.15],[x+.045,.29,z-.14],[x-.045,.29,z-.14]],'#9daa9e');
          polygon([[x-.13,tower-.11,z-.23],[x+.13,tower-.11,z-.23],[x+.13,tower+.13,z-.23],[x-.13,tower+.13,z-.23]],'#e4ebe3');
          polygon([[x+.13,tower-.11,z-.23],[x+.13,tower-.11,z+.36],[x+.13,tower+.13,z+.36],[x+.13,tower+.13,z-.23]],'#94a79b');
          polygon([[x-.13,tower+.13,z-.23],[x+.13,tower+.13,z-.23],[x+.13,tower+.13,z+.36],[x-.13,tower+.13,z+.36]],'#fbfcf5');
          const phase=rotorPhase+index*.7;
          for(let blade=0;blade<3;blade++){const angle=phase+blade*Math.PI*2/3,dx=Math.sin(angle),dy=Math.cos(angle),px=Math.cos(angle),py=-Math.sin(angle),wing=(distance,side,depth)=>[x+dx*distance+px*side,tower+dy*distance+py*side,z-.25+depth];
            polygon([wing(.05,-.035,0),wing(.25,.105,0),wing(radius,.025,0),wing(radius,0,0),wing(.32,-.045,0)],'#fbfcf8','#bccbc1');
            polygon([wing(.12,-.035,0),wing(.32,-.045,0),wing(radius,0,0),wing(.28,.025,0)],'#b7c6bc');}
          const p=project([x,tower,z-.28]),hubSize=Math.max(3,p[2]*.1),hubLight=ctx.createRadialGradient(p[0]-hubSize*.4,p[1]-hubSize*.4,0,p[0],p[1],hubSize);hubLight.addColorStop(0,'#ffffff');hubLight.addColorStop(.65,'#e9efe7');hubLight.addColorStop(1,'#9bae9f');ctx.beginPath();ctx.arc(p[0],p[1],hubSize,0,Math.PI*2);ctx.fillStyle=hubLight;ctx.fill();
          ctx.font='600 14px -apple-system, Segoe UI, sans-serif';ctx.fillStyle=accent;ctx.textAlign='center';ctx.fillText('T'+(index+1),base[0],base[1]+23);hubs[index]=p;});
        panels.forEach(({panel},i)=>{const hub=hubs[i];if(!hub)return;const panelRect=panel.getBoundingClientRect(),stageRect=stage.getBoundingClientRect(),sx=panelRect.left-stageRect.left+panelRect.width*.5,sy=panelRect.bottom-stageRect.top+6;ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(sx,sy+12);ctx.lineTo(hub[0],hub[1]);ctx.setLineDash([3,5]);ctx.strokeStyle='rgba(20,110,90,.35)';ctx.lineWidth=1;ctx.stroke();ctx.setLineDash([]);});
      }
      if(playing||(!reduced.matches&&!mapActive)||resetting)frame=requestAnimationFrame(render);
    }
    function schedule(){if(!frame&&!destroyed&&visible)frame=requestAnimationFrame(render);}
    function resize(){const rect=canvas.getBoundingClientRect();width=rect.width;height=rect.height;const ratio=Math.min(window.devicePixelRatio||1,2);canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);if(ctx)ctx.setTransform(ratio,0,0,ratio,0,0);if(scene3d)scene3d.resize();schedule();}
    play.addEventListener('click',()=>{playing=!playing;play.textContent=playing?'Пауза':'Пуск';play.setAttribute('aria-pressed',String(playing));play.setAttribute('aria-label',playing?'Приостановить прогноз':'Воспроизвести прогноз по часам');lastStep=performance.now();schedule();});
    slider.addEventListener('input',()=>{active=Number(slider.value);lastStep=performance.now();update();schedule();});
    reset.addEventListener('click',()=>{if(mapActive){mapController.reset();return;}resetting=true;if(reduced.matches){yaw=0;elevation=0;resetting=false;}schedule();});
    canvas.addEventListener('pointerdown',event=>{if(event.button!==0||mapActive)return;resetting=false;drag={x:event.clientX,y:event.clientY,yaw,elevation};canvas.setPointerCapture(event.pointerId);});
    canvas.addEventListener('pointermove',event=>{if(!drag)return;const limit=scene3d?1.3:.3;yaw=Math.max(-limit,Math.min(limit,drag.yaw+(event.clientX-drag.x)*.003));if(event.pointerType!=='touch')elevation=Math.max(-5,Math.min(12,drag.elevation+(event.clientY-drag.y)*.045));schedule();});
    for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,()=>{drag=null;});
    const observer=typeof ResizeObserver!=='undefined'?new ResizeObserver(resize):null;if(observer)observer.observe(canvas);else window.addEventListener('resize',resize);
    const visibility=typeof IntersectionObserver!=='undefined'?new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;if(visible)schedule();else{cancelAnimationFrame(frame);frame=0;}}):null;if(visibility)visibility.observe(stage);
    if(!ctx)canvas.hidden=true;
    import(moduleUrl).then(THREE=>{
      if(destroyed)return;
      const renderer=new THREE.WebGLRenderer({antialias:true,alpha:false,powerPreference:'high-performance'});
      renderer.domElement.className='forecast3d-webgl';stage.insertBefore(renderer.domElement,canvas);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,1.75));
      renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
      renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1;
      const scene=new THREE.Scene();scene.background=new THREE.Color('#d8e2e4');scene.fog=new THREE.FogExp2('#d8e2e4',.0125);
      const skyMaterial=new THREE.ShaderMaterial({side:THREE.BackSide,depthWrite:false,toneMapped:false,precision:'highp',uniforms:{zenith:{value:new THREE.Color('#5c9ecb')},horizon:{value:new THREE.Color('#d5e6ec')}},vertexShader:`varying vec3 skyDirection;void main(){skyDirection=position;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}`,fragmentShader:`uniform vec3 zenith;uniform vec3 horizon;varying vec3 skyDirection;
        float cloudPatch(vec2 uv,vec2 center,vec2 stretch){vec2 p=(uv-center)*stretch;return exp(-dot(p,p));}
        void main(){vec3 d=normalize(skyDirection);float h=max(d.y,0.0);vec3 color=mix(horizon,zenith,pow(h,.28));vec2 uv=vec2(d.x,h);
          float cloud=.58*cloudPatch(uv,vec2(-.29,.22),vec2(9.,42.))+.44*cloudPatch(uv,vec2(.05,.27),vec2(12.,48.))+.32*cloudPatch(uv,vec2(.35,.17),vec2(10.,45.));
          cloud*=smoothstep(.1,.8,-d.z);color=mix(color,vec3(.95,.97,.98),min(cloud,.68));float glow=pow(max(dot(d,normalize(vec3(-.5,.7,.4))),0.),32.);color+=vec3(.055,.047,.03)*glow;gl_FragColor=vec4(color,1.);
        #include <colorspace_fragment>
        }`});
      scene.add(new THREE.Mesh(new THREE.SphereGeometry(180,96,64),skyMaterial));
      const camera=new THREE.PerspectiveCamera(43,1,.1,240);
      scene.add(new THREE.HemisphereLight('#edf4f1','#8a8665',1.45));
      const sun=new THREE.DirectionalLight('#fff4de',2.8);sun.position.set(-24,42,25);sun.castShadow=true;
      sun.shadow.mapSize.set(2048,2048);sun.shadow.camera.left=-30;sun.shadow.camera.right=30;sun.shadow.camera.top=28;sun.shadow.camera.bottom=-28;sun.shadow.camera.near=1;sun.shadow.camera.far=110;sun.shadow.normalBias=.035;sun.shadow.bias=-.0002;sun.shadow.radius=4;scene.add(sun);
      const mesh=(geometry,material,parent=scene)=>{const shape=new THREE.Mesh(geometry,material);shape.castShadow=true;shape.receiveShadow=true;parent.add(shape);return shape;};
      const ground=new THREE.PlaneGeometry(230,230,100,100);ground.rotateX(-Math.PI/2);
      const vertices=ground.attributes.position;
      const terrainHeight=(x,z)=>{const distance=Math.max(0,(-z-35)/55),mountain=distance*(6+Math.sin(x*.085)*4+Math.sin(x*.19+z*.08)*3+Math.sin(x*.38+z*.21));return Math.sin(x*.15)*Math.sin(z*.12)*.15+Math.max(0,mountain);};
      for(let i=0;i<vertices.count;i++){
        const x=vertices.getX(i),z=vertices.getZ(i);vertices.setY(i,terrainHeight(x,z));
      }
      ground.computeVertexNormals();
      const terrainCanvas=document.createElement('canvas');terrainCanvas.width=terrainCanvas.height=256;
      const terrainContext=terrainCanvas.getContext('2d'),grain=terrainContext.createImageData(256,256);
      for(let i=0;i<256*256;i++){const noise=Math.sin(i*12.9898)*43758.5453,value=185+(noise-Math.floor(noise))*48;grain.data[i*4]=value;grain.data[i*4+1]=value;grain.data[i*4+2]=value;grain.data[i*4+3]=255;}
      terrainContext.putImageData(grain,0,0);
      const terrainTexture=new THREE.CanvasTexture(terrainCanvas);terrainTexture.wrapS=terrainTexture.wrapT=THREE.RepeatWrapping;terrainTexture.repeat.set(34,34);terrainTexture.colorSpace=THREE.SRGBColorSpace;
      const terrainColors=[];const dry=new THREE.Color('#c1ab79'),dust=new THREE.Color('#9c8d67'),rock=new THREE.Color('#9d9c8e'),tint=new THREE.Color();
      for(let i=0;i<vertices.count;i++){const x=vertices.getX(i),z=vertices.getZ(i),patch=(Math.sin(x*.13+Math.sin(z*.21))*Math.cos(z*.095)+1)*.5;tint.copy(dry).lerp(dust,patch*.65);if(z<-25)tint.lerp(rock,Math.min((-z-25)/70,.85));terrainColors.push(tint.r,tint.g,tint.b);}
      ground.setAttribute('color',new THREE.Float32BufferAttribute(terrainColors,3));
      const groundMat=new THREE.MeshStandardMaterial({vertexColors:true,map:terrainTexture,roughness:1,metalness:0});mesh(ground,groundMat).castShadow=false;
      const gravelMat=new THREE.MeshStandardMaterial({color:'#a4a78f',roughness:1});
      const pebbleGeometry=new THREE.IcosahedronGeometry(.12,0);
      const pebbles=new THREE.InstancedMesh(pebbleGeometry,gravelMat,190);
      const transform=new THREE.Object3D();
      for(let i=0;i<190;i++){const x=((i*73)%997)/997*80-40,z=((i*47)%991)/991*80-30;transform.position.set(x,.02,z);transform.rotation.set(i*.7,i*.4,i*.13);transform.scale.set(1+(i%3),.4,1);transform.updateMatrix();pebbles.setMatrixAt(i,transform.matrix);}pebbles.receiveShadow=true;scene.add(pebbles);
      const windGeometry=new THREE.BufferGeometry(),windPositions=new Float32Array(streamCount*streamSegments*6),windFades=new Float32Array(streamCount*streamSegments*2);
      windGeometry.setAttribute('position',new THREE.BufferAttribute(windPositions,3));
      for(let stream=0;stream<streamCount;stream++)for(let segment=0;segment<streamSegments;segment++){const offset=(stream*streamSegments+segment)*2;windFades[offset]=Math.pow(segment/streamSegments,1.6);windFades[offset+1]=Math.pow((segment+1)/streamSegments,1.6);}
      windGeometry.setAttribute('fade',new THREE.BufferAttribute(windFades,1));
      const windMaterial=new THREE.ShaderMaterial({transparent:true,depthWrite:false,toneMapped:false,vertexShader:`attribute float fade;varying float trailFade;void main(){trailFade=fade;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}`,fragmentShader:`varying float trailFade;void main(){gl_FragColor=vec4(.98,.99,.96,trailFade*.62);}`});
      const windLines=new THREE.LineSegments(windGeometry,windMaterial);windLines.frustumCulled=false;scene.add(windLines);
      const towerMat=new THREE.MeshStandardMaterial({color:'#f0f2e9',roughness:.36,metalness:.17});
      const edgeMat=new THREE.MeshStandardMaterial({color:'#aebbb2',roughness:.55,metalness:.25});
      const baseMat=new THREE.MeshStandardMaterial({color:'#c2c4b5',roughness:.9});
      const turbines=[];
      [-1,1].forEach((side,index)=>{
        const group=new THREE.Group();group.position.set(side*6.2,0,index?-5:2);scene.add(group);
        mesh(new THREE.CylinderGeometry(1.1,1.3,.25,32),baseMat,group).position.y=.2;
        mesh(new THREE.CylinderGeometry(.27,.59,12.5,40),towerMat,group).position.y=6.4;
        const door=mesh(new THREE.BoxGeometry(.35,.7,.03),edgeMat,group);door.position.set(0,.7,.58);
        const nacelle=mesh(new THREE.BoxGeometry(1.05,.85,2.15),towerMat,group);nacelle.position.set(0,12.8,-.32);
        const cap=mesh(new THREE.SphereGeometry(.52,20,12),towerMat,group);cap.position.set(0,12.8,.73);cap.scale.set(1,.85,.65);
        const rotor=new THREE.Group();rotor.position.set(0,12.8,1.05);group.add(rotor);
        mesh(new THREE.SphereGeometry(.34,24,16),towerMat,rotor).scale.z=1.6;
        const bladeShape=new THREE.Shape();bladeShape.moveTo(-.12,.24);bladeShape.bezierCurveTo(-.32,.8,-.35,1.45,-.22,2.1);bladeShape.lineTo(.02,5.4);bladeShape.quadraticCurveTo(.11,5.55,.15,5.3);bladeShape.lineTo(.42,1.45);bladeShape.quadraticCurveTo(.42,.72,.12,.24);bladeShape.closePath();
        const bladeGeometry=new THREE.ExtrudeGeometry(bladeShape,{depth:.11,bevelEnabled:true,bevelSegments:2,steps:1,bevelSize:.035,bevelThickness:.035,curveSegments:12});
        for(let blade=0;blade<3;blade++){const wing=mesh(bladeGeometry,towerMat,rotor);wing.rotation.z=blade*Math.PI*2/3;wing.rotation.y=.06;}
        turbines.push({group,rotor});
      });
      const look=new THREE.Vector3(0,7.6,-1),hub=new THREE.Vector3();
      const resizeGL=()=>{renderer.setSize(width,height,false);camera.aspect=width/height;camera.updateProjectionMatrix();};
      renderer.domElement.addEventListener('webglcontextlost',event=>{event.preventDefault();renderer.domElement.style.display='none';scene3d=null;schedule();});
      scene3d={resize:resizeGL,render(stamp,angle){
        const mobile=width<600,orbit=angle*1.5+.08,radius=mobile?Math.max(58,72-(height-650)*.078):35;
        look.set(0,mobile?11.8:7.6,-1);
        camera.position.set(Math.sin(orbit)*radius,(mobile?15.5:14.5)+elevation,Math.cos(orbit)*radius);
        camera.up.set(0,1,0);camera.lookAt(look);
        // Север сцены — минус Z; метеорологический угол задаёт, откуда дует ветер.
        const dx=Math.sin(flowAngle),dz=Math.cos(flowAngle);
        turbines.forEach(({group,rotor},index)=>{group.position.x=(index?1:-1)*(mobile?4:6.2);rotor.rotation.z=rotorPhase+index*.6;});
        for(const stream of streams){const i=stream.index;
          for(let segment=0;segment<streamSegments;segment++)for(let endpoint=0;endpoint<2;endpoint++){const t=(segment+endpoint)/streamSegments,along=(t-1)*4.2,bend=Math.sin(t*2.2+i*.8)*.28,offset=(i*streamSegments+segment)*6+endpoint*3;windPositions[offset]=stream.x+along*dx-bend*dz;windPositions[offset+1]=stream.y+Math.sin(t*2+i)*.12;windPositions[offset+2]=stream.z+along*dz+bend*dx;windFades[(i*streamSegments+segment)*2+endpoint]=Math.pow(t,1.6)*stream.fade;}}
        windGeometry.attributes.fade.needsUpdate=true;
        windGeometry.attributes.position.needsUpdate=true;
        renderer.render(scene,camera);
        if(ctx){ctx.clearRect(0,0,width,height);panels.forEach(({panel},index)=>{
          turbines[index].group.localToWorld(hub.set(0,12.8,1.05));hub.project(camera);
          const hx=(hub.x*.5+.5)*width,hy=(-hub.y*.5+.5)*height,panelRect=panel.getBoundingClientRect(),stageRect=stage.getBoundingClientRect(),sx=panelRect.left-stageRect.left+panelRect.width*.5,sy=panelRect.bottom-stageRect.top+6;
          ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(sx,sy+12);ctx.lineTo(hx,hy);ctx.setLineDash([3,5]);ctx.strokeStyle='rgba(20,110,90,.38)';ctx.lineWidth=1;ctx.stroke();ctx.setLineDash([]);
          ctx.font='600 14px -apple-system, Segoe UI, sans-serif';ctx.fillStyle=accent;ctx.textAlign='center';
          turbines[index].group.localToWorld(hub.set(0,0,1.05));hub.project(camera);ctx.fillText('T'+(index+1),(hub.x*.5+.5)*width-(mobile?0:42),(-hub.y*.5+.5)*height+(mobile?22:-4));
        });}
      },destroy(){terrainTexture.dispose();scene.traverse(shape=>{if(shape.geometry)shape.geometry.dispose();if(shape.material){for(const material of Array.isArray(shape.material)?shape.material:[shape.material])material.dispose();}});renderer.dispose();renderer.domElement.remove();}};
      root.dataset.renderer='webgl';resizeGL();schedule();
    }).catch(()=>{root.dataset.renderer='canvas';});
    update();resize();
    return {resize,getIssueTime:()=>selected[0],destroy(){destroyed=true;cancelAnimationFrame(frame);if(scene3d)scene3d.destroy();if(mapController)mapController.destroy();if(observer)observer.disconnect();else window.removeEventListener('resize',resize);if(visibility)visibility.disconnect();root.remove();}};
  }
  window.Forecast3D={mount};
})();
