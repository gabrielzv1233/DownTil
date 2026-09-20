(()=>{
  'use strict';
  const config=window.DOWNTIL_JOB;
  if(!config)return;
  const $=id=>document.getElementById(id);
  const elements={stage:$('stage'),queue:$('queue'),qpos:$('qpos'),bar:$('bar'),wrap:$('progressWrap'),pct:$('pct'),speed:$('speed'),eta:$('eta'),done:$('done'),download:$('download'),error:$('err'),scope:$('scope')};
  let failures=0,autoStarted=false,finished=false;
  const readableSpeed=value=>{
    let n=Number(value),i=0;
    if(!Number.isFinite(n)||n<=0)return 'Speed unavailable';
    while(n>=1024&&i<4){n/=1024;i++}
    return n.toFixed(1)+' '+['B/s','KB/s','MB/s','GB/s','TB/s'][i];
  };
  const readableTime=value=>{
    const n=Math.max(0,Math.ceil(value));
    if(n<60)return n+'s';
    if(n<3600)return Math.floor(n/60)+'m '+(n%60)+'s';
    return Math.floor(n/3600)+'h '+Math.floor(n%3600/60)+'m';
  };
  const schedule=delay=>{if(!finished)window.setTimeout(poll,delay)};
  const setProgress=(percent,scope)=>{
    const known=typeof percent==='number'&&Number.isFinite(percent);
    elements.wrap.classList.toggle('indeterminate',!known);
    elements.scope.textContent=scope||'Current file';
    if(known){
      const p=Math.max(0,Math.min(100,percent));
      elements.bar.style.width=p+'%';
      elements.pct.textContent=p.toFixed(1)+'%';
      elements.pct.hidden=false;
      elements.wrap.setAttribute('aria-valuenow',p.toFixed(1));
      elements.wrap.setAttribute('aria-valuetext',(scope||'Current file')+' '+p.toFixed(1)+' percent');
    }else{
      elements.bar.style.width='';
      elements.pct.hidden=true;
      elements.wrap.removeAttribute('aria-valuenow');
      elements.wrap.setAttribute('aria-valuetext',scope||'Progress cannot be estimated');
    }
  };
  const render=job=>{
    elements.stage.textContent=job.detail||job.stage||'Working…';
    elements.queue.hidden=job.stage!=='queued';
    elements.qpos.textContent=job.queue_position??0;
    const ready=Boolean(job.ready&&job.file_url);
    setProgress(ready?100:job.progress,ready?'Complete':job.progress_scope);
    elements.speed.textContent=job.stage==='downloading'?readableSpeed(job.speed):'';
    elements.speed.hidden=job.stage!=='downloading';
    if(job.stage==='downloading'){
      elements.eta.textContent=Number.isFinite(job.eta)&&job.eta>=0?'Current file ETA: '+(job.estimated?'~':'')+readableTime(job.eta):'Current file ETA unavailable';
    }else if(job.stage==='processing'){
      elements.eta.textContent='Processing time unavailable';
    }else{
      elements.eta.textContent='';
    }
    elements.eta.hidden=!elements.eta.textContent;
    if(job.error){
      elements.error.textContent=job.error;
      elements.error.hidden=false;
      finished=true;
      return;
    }
    elements.error.hidden=true;
    if(ready){
      elements.done.hidden=false;
      elements.download.href=job.file_url;
      finished=true;
      if(config.auto&&!autoStarted){
        autoStarted=true;
        const link=document.createElement('a');
        link.href=job.file_url;
        link.download='';
        document.body.append(link);
        link.click();
        link.remove();
      }
    }
  };
  async function poll(){
    if(finished)return;
    if(document.hidden){schedule(2500);return}
    try{
      const response=await fetch('/job/'+encodeURIComponent(config.id)+'/status',{cache:'no-store'});
      if(!response.ok)throw new Error('Status request failed');
      const job=await response.json();
      failures=0;
      render(job);
      schedule(job.stage==='queued'?2000:1000);
    }catch(error){
      failures++;
      elements.error.hidden=false;
      elements.error.textContent='Connection interrupted. Retrying…';
      schedule(Math.min(12000,1000*2**Math.min(failures,4)));
    }
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!finished)poll()});
  poll();
})();
