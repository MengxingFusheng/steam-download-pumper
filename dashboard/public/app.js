'use strict';
const $ = id => document.getElementById(id);
const bytes = n => { if (n == null) return '未知'; let v=Number(n),i=0;const units=['B','GiB','TiB','PiB']; if(v<1024**3)return (v/1024**2).toFixed(1)+' MiB';v/=1024**3;i=1;while(v>=1024&&i<3){v/=1024;i++;}return v.toFixed(2)+' '+units[i]; };
const stamp = t => t ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(t*1000)):'尚未上报';
const clock = t => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(t*1000));
const dayNow = () => new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const completion = {achieved:'已达标',unmet:'未达标',pending:'待结算',unknown:'目标未知',not_applicable:'不适用',no_target:'无目标'};
const phaseNames = {daytime:'00–18 点 · 日间补量',upload_only:'上传专用时段',late_makeup:'23–24 点 · 晚间补量',legacy_00_01:'00–01 点',legacy_01_07:'01–07 点',legacy_07_18:'07–18 点'};
const commandStates={queued:'等待节点领取',dispatched:'等待执行回执',succeeded:'执行成功',failed:'执行失败',unknown:'结果未知，请检查节点',expired:'已过期'};
const fieldLabels={target_ratio:'目标下载 / 上传倍率',rotation_minutes:'轮转分钟',router_limit_mbps:'WAN 总预算 Mbps',router_reserve_mbps:'WAN 预留 Mbps',late_download_limit_mbps:'晚间补量预算 Mbps',line_stall_seconds:'无进度换线秒数',line_slow_window_seconds:'低速判断秒数',line_failure_cooldown_minutes:'故障冷却分钟'};
let nodeId='',current=null,nodePage=0,nodeTotal=0,linePage=0,busy=false,formDirty=false,refreshing=false,formVersion="",formBaseline={};
function el(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
function fail(error){const translations={'configuration conflict; refresh first':'节点配置已变化，请刷新页面后重新编辑。','configuration conflict; refresh and retry':'节点配置已变化，请刷新后重新提交。','node offline':'节点离线，暂时无法下发操作。','another command is pending':'已有操作等待节点完成。','authentication required':'需要管理员登录。','command expired':'命令已过期。','invalid node credentials':'节点凭据无效。'};const message=error.message||String(error);$('error').hidden=false;$('error').textContent=translations[message]||message;}
async function api(path,options={}){const response=await fetch('/api/v1'+path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json','X-Requested-With':'FleetDashboard',...options.headers}});let data;try{data=await response.json();}catch{throw new Error('服务返回异常，请检查连接');}if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data;}
function kpi(label,value){const n=el('article');n.append(el('span',label),el('strong',value));return n;}
function progress(actual,target){return target==null?'目标未知':target===0?'无目标':actual==null?'实际量未知':(actual/target*100).toFixed(1)+'%';}
const mbps = n => n == null ? '未知' : Number(n).toLocaleString('zh-CN',{maximumFractionDigits:2})+' Mbps';
let progressSequence=0,refreshPending=false;
const progressParts=[['measured','实测下载','#3b82f6'],['estimated','估算下载','#f59e0b'],['legacy','历史口径','#8b5cf6'],['unclassified','未分项下载','#64748b'],['remaining','未达目标','#cbd5e1']];
function targetDescription(snapshot){
  if(snapshot?.mode==='multi_ip')return '配置下载速度 × 时段时长';
  const ratio=Number(snapshot?.config?.target_ratio);
  const times=Number.isFinite(ratio)&&ratio>0?ratio.toLocaleString('zh-CN',{maximumFractionDigits:3})+' 倍':'倍率未知';
  return (snapshot?.mode==='daily'?'预计上传量':'当日累计上传量')+' × '+times;
}
function uploadView(snapshot,online){
  const value=snapshot.summary?.upload_mbps;
  if(value!=null)return {label:online?'当前上传速度':'最后上报上传速度',value:mbps(value)};
  const enabled=(snapshot.lines||[]).filter(line=>line.enabled),known=enabled.filter(line=>line.current_upload_mbps!=null);
  if(known.length)return {label:(online?'已测上传速度':'最后上报已测上传')+' · '+known.length+'/'+enabled.length+' 线路',value:mbps(known.reduce((sum,line)=>sum+line.current_upload_mbps,0))};
  return {label:online?'当前上传速度':'最后上报上传速度',value:'未知'};
}
function renameButton(node){
  const button=el('button','改名','secondary rename-node');button.type='button';
  button.setAttribute('aria-label','编辑节点名称');
  button.onclick=()=>window.dispatchEvent(new CustomEvent('fleet-rename-node',{detail:{id:node.id,name:node.name}}));
  return button;
}
function completionMeter(actual,target,label='目标完成度',unavailable='',quality=null){
  const known=!unavailable&&actual!=null&&target!=null&&target>0;
  const ratio=known?actual/target*100:0,text=unavailable||progress(actual,target);
  const meter=el('div',null,'completion-meter'+(known&&ratio>=100?' achieved':''));
  const heading=el('div',null,'completion-heading');
  heading.append(el('span',label),el('strong',text));meter.append(heading);
  if(known){
    const track=el('div',null,'glass-track'),bar=el('progress');bar.max=100;bar.value=Math.max(0,Math.min(100,ratio));
    bar.setAttribute('role','progressbar');bar.setAttribute('aria-label',label);
    bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');
    bar.setAttribute('aria-valuenow',String(bar.value));bar.setAttribute('aria-valuetext',text);
    const segments=svg('svg',{viewBox:'0 0 100 20',preserveAspectRatio:'none',class:'progress-segments','aria-hidden':'true'});
    const defs=svg('defs'),prefix='completion-'+(++progressSequence)+'-';
    const amounts={measured:Math.max(0,Number(quality?.measured_bytes)||0),estimated:Math.max(0,Number(quality?.estimated_bytes)||0),legacy:Math.max(0,Number(quality?.legacy_bytes)||0)};
    const classified=amounts.measured+amounts.estimated+amounts.legacy;
    // Inconsistent quality totals must not invent the composition of actual bytes.
    if(classified>actual){amounts.measured=amounts.estimated=amounts.legacy=0;}
    amounts.unclassified=Math.max(0,actual-amounts.measured-amounts.estimated-amounts.legacy);
    amounts.remaining=Math.max(0,target-actual);
    const basis=Math.max(actual,target),legend=el('div',null,'progress-legend');let left=0;
    for(const [key,name,color] of progressParts){
      const gradient=svg('linearGradient',{id:prefix+key,x1:'0',y1:'0',x2:'0',y2:'1'});
      gradient.append(svg('stop',{offset:'0%','stop-color':color,'stop-opacity':key==='remaining'?0.3:0.45}),svg('stop',{offset:'100%','stop-color':color,'stop-opacity':key==='remaining'?0.5:0.9}));defs.append(gradient);
      const width=amounts[key]/basis*100;
      if(width>0){
        segments.append(svg('rect',{x:left,y:0,width,height:20,fill:'url(#'+prefix+key+')','data-quality':key}));
        const item=el('span',null,'legend-item '+key);item.append(el('i',null,'legend-dot'),document.createTextNode(name));
        item.title=name+' '+bytes(amounts[key]);legend.append(item);left+=width;
      }
    }
    const shine=svg('linearGradient',{id:prefix+'shine',x1:'0',y1:'0',x2:'0',y2:'1'});
    shine.append(svg('stop',{offset:'0%','stop-color':'white','stop-opacity':0.65}),svg('stop',{offset:'48%','stop-color':'white','stop-opacity':0.05}),svg('stop',{offset:'100%','stop-color':'white','stop-opacity':0.12}));defs.append(shine);segments.prepend(defs);
    segments.append(svg('rect',{x:0,y:0,width:100,height:20,fill:'url(#'+prefix+'shine)'}));
    track.append(bar,segments);meter.append(track,legend);
  }else{meter.append(el('div',null,'completion-track glass-track'));}
  return meter;
}
async function overview(){
  const query=new URLSearchParams({q:$('search').value,status:$('status').value,offset:nodePage*25,limit:25});
  const data=await api('/nodes?'+query);nodeTotal=data.total;$('nodes').replaceChildren();
  for(const node of data.nodes){
    const s=node.snapshot.summary||{},card=el('article',null,'node'),title=el('button',node.name,'link');
    title.onclick=()=>{location.hash=node.id;};
    card.append(el('span',node.online?'在线':'离线 · 最后上报','badge '+(node.online?'online':'offline')),el('h2'));
    card.lastChild.className='node-title';card.lastChild.append(title,renameButton(node));card.append(el('p',node.external_id||node.id,'muted'));
    const values=el('div',null,'values');
    for(const [label,value] of [[(node.snapshot.mode==='multi_ip'?(node.snapshot.stats_source==='interface_fallback'?'当日下载 · 接口统计':'当日下载 · 程序统计'):'当日实际下载'),s.actual_bytes],['预计下载目标',s.target_bytes]]){
      const n=el('div',label);n.append(el('strong',bytes(value)));values.append(n);
    }
    card.append(values,completionMeter(s.actual_bytes,s.target_bytes,'当日下载完成度','',s));card.append(yesterdayTotals(node.yesterday));
    const uploads=el('div',null,'values uploads'),liveUpload=uploadView(node.snapshot,node.online);
    for(const [label,value] of [['当日累计上传',bytes(s.upload_bytes)],[liveUpload.label,liveUpload.value]]){
      const n=el('div',label);n.append(el('strong',value));uploads.append(n);
    }
    card.append(uploads,el('p','目标下载 = '+targetDescription(node.snapshot),'muted'),el('p','最后心跳 '+stamp(node.last_seen),'muted'));
    $('nodes').append(card);
  }
  if(!data.nodes.length)$('nodes').append(el('p','暂无节点。配置节点的 DASHBOARD_URL 和注册凭据后，节点将自动出现在这里。','notice'));
  $('page').textContent=`${nodePage+1} / ${Math.max(1,Math.ceil(data.total/25))}`;
  $('prev').disabled=nodePage===0;$('next').disabled=(nodePage+1)*25>=data.total;
}
function options(select,values,label){const selected=select.value;select.replaceChildren(el('option',label));select.firstChild.value='';for(const [value,text] of values){const o=el('option',text);o.value=value;select.append(o);}select.value=[...select.options].some(o=>o.value===selected)?selected:'';}
function renderLines(){
  const lines=(current.snapshot.lines||[]).filter(line=>!$('groupFilter').value||line.group_id===$('groupFilter').value);
  linePage=Math.min(linePage,Math.max(0,Math.ceil(lines.length/50)-1));$('lineRows').replaceChildren();
  for(const line of lines.slice(linePage*50,(linePage+1)*50)){
    const tr=el('tr'),name=el('td'),button=el('button',line.line_id,'link');
    button.onclick=()=>{$('chartLine').value=line.line_id;loadSeries().catch(fail);loadPeriods().catch(fail);$('chartLine').focus();};
    name.append(button,el('small',line.bind_ip+' · '+line.snmp_interface));
    const state=el('td',line.group_id);state.append(el('small',line.enabled?(line.active?'下载中':line.status):'已禁用'));
    const lineKnown=line.metrics_available!==false,target=el('td',bytes(line.target_bytes),'line-completion');
    target.append(el('small','按 '+targetDescription(current.snapshot)),completionMeter(lineKnown?line.actual_bytes:null,line.target_bytes,'下载完成度',lineKnown?'':'线路统计未知',line));
    const upload=el('td',bytes(line.upload_bytes),'line-upload');
    upload.append(el('small',(current.online?'当前 ':'最后上报 ')+mbps(line.current_upload_mbps)));
    tr.append(name,state,el('td',bytes(lineKnown?line.actual_bytes:null)),target,upload,el('td',bytes(lineKnown?line.program_bytes:null)),el('td',current.snapshot.mode==='multi_ip'?(lineKnown?'程序统计 · 未接入 SNMP':'线路统计不可用'):`实测 ${bytes(line.measured_bytes)} / 估算 ${bytes(line.estimated_bytes)} / 未知 ${line.unknown_seconds}s`));
    $('lineRows').append(tr);
  }
  $('linePage').textContent=`${linePage+1} / ${Math.max(1,Math.ceil(lines.length/50))} · ${lines.length} 条`;
  $('linePrev').disabled=linePage===0;$('lineNext').disabled=(linePage+1)*50>=lines.length;
}
function svg(tag,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n;}
function yesterdayTotals(value){
  const box=el('div',null,'daily-yesterday');
  box.append(el('small','昨日 · '+(value?.date?.slice(5).replace('-','/')||'等待上报')));
  for(const [key,label,kind] of [['download_bytes','下载','download'],['upload_bytes','上传','upload']]){
    const row=el('span',null,'daily-total-'+kind);
    row.append(el('span',label),el('strong',value?.[key]==null?'缺失':bytes(value[key])));box.append(row);
  }
  return box;
}
function renderDailyTraffic(days,yesterday){
  let panel=$('dailyTrafficPanel');
  if(!panel){
    panel=el('article',null,'panel daily-traffic-panel');panel.id='dailyTrafficPanel';
    $('chart').closest('article').before(panel);
  }
  panel.dataset.nodeId=nodeId;
  panel.replaceChildren(el('h2','最近 14 天上传下载总量'),el('p','北京时间 · 截至昨日的完整自然日 · 已记录量含估算，缺失不补零','muted'),yesterdayTotals(yesterday));
  const legend=el('div',null,'legend');legend.append(el('span','● 下载','daily-total-download'),el('span','● 上传','daily-total-upload'));panel.append(legend);
  const scroll=el('div',null,'daily-bars-scroll'),chart=svg('svg',{viewBox:'0 0 1000 300',role:'group','aria-label':'最近14个完整自然日上传、下载已记录总量柱状图'});
  const tooltip=el('div',null,'fleet-chart-tooltip');tooltip.id='dailyTrafficTooltip';tooltip.setAttribute('role','tooltip');tooltip.hidden=true;
  const maximum=Math.max(1,...days.flatMap(day=>[day.download_bytes||0,day.upload_bytes||0]))*1.12;
  const unit=maximum>=1024**4?1024**4:1024**3,unitName=unit===1024**4?'TiB':'GiB',step=920/Math.max(1,days.length),height=value=>value/maximum*210;
  for(let i=0;i<=4;i++){
    const yy=240-i*210/4;chart.append(svg('line',{x1:60,x2:980,y1:yy,y2:yy,class:'daily-grid'}));
    const text=svg('text',{x:52,y:yy+4,'text-anchor':'end'});text.textContent=(maximum*i/4/unit).toFixed(1);chart.append(text);
  }
  const axis=svg('text',{x:8,y:16});axis.textContent=unitName;chart.append(axis);
  const show=(day,index)=>{
    tooltip.replaceChildren(el('strong',day.date+' · 北京时间'));
    for(const [key,label,kind] of [['download_bytes','下载','download'],['upload_bytes','上传','upload']]){
      const row=el('div',null,'fleet-chart-tooltip-row');row.append(el('span',label,'daily-total-'+kind),el('b',day[key]==null?'缺失':bytes(day[key])));tooltip.append(row);
    }
    const estimated=(day.estimated_upload_bytes||0)+(day.estimated_download_bytes||0);
    tooltip.append(el('small',estimated?'含估算：下载 '+bytes(day.estimated_download_bytes)+' / 上传 '+bytes(day.estimated_upload_bytes):'已记录账本总量'));
    tooltip.append(el('small','记录 '+(day.observed_minutes||0)+' / 1440 分钟 · 未知 '+(day.unknown_seconds||0)+' 秒'));
    tooltip.hidden=false;
    const anchor=chart.createSVGPoint();anchor.x=60+(index+.5)*step;anchor.y=30;
    const at=anchor.matrixTransform(chart.getScreenCTM()),bounds=panel.getBoundingClientRect();
    tooltip.style.left=Math.max(0,Math.min(at.x-bounds.left+10,panel.clientWidth-tooltip.offsetWidth))+'px';
    tooltip.style.top=Math.max(0,at.y-bounds.top+8)+'px';
  };
  days.forEach((day,index)=>{
    const group=svg('g',{tabindex:0,role:'img','aria-label':day.date+'，下载 '+(day.download_bytes==null?'缺失':bytes(day.download_bytes))+'，上传 '+(day.upload_bytes==null?'缺失':bytes(day.upload_bytes)),'data-date':day.date});
    for(const [key,kind,offset] of [['download_bytes','download',-21],['upload_bytes','upload',1]]){
      const value=day[key],xx=60+(index+.5)*step+offset;
      if(value==null){const mark=svg('text',{x:xx+9,y:234,'text-anchor':'middle',class:'daily-missing'});mark.textContent='—';group.append(mark);}
      else group.append(svg('rect',{x:xx,y:240-Math.max(1,height(value)),width:18,height:Math.max(1,height(value)),rx:2,class:'daily-bar-'+kind,'data-bytes':value}));
    }
    group.append(svg('rect',{x:60+index*step,y:25,width:step,height:220,fill:'transparent'}));
    const label=svg('text',{x:60+(index+.5)*step,y:266,'text-anchor':'middle'});label.textContent=day.date.slice(5).replace('-','/');group.append(label);
    group.addEventListener('pointermove',()=>show(day,index));group.addEventListener('focus',()=>show(day,index));group.addEventListener('blur',()=>{tooltip.hidden=true;});group.addEventListener('keydown',event=>{if(event.key==='Escape')tooltip.hidden=true;});chart.append(group);
  });
  chart.addEventListener('pointerleave',()=>{tooltip.hidden=true;});scroll.append(chart);panel.append(scroll,tooltip);
  const details=el('details'),wrap=el('div',null,'table-wrap'),table=el('table'),head=el('tr');
  for(const title of ['日期（北京时间）','下载总量','上传总量','估算下载 / 上传','记录分钟 / 未知秒数'])head.append(el('th',title));
  const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');body.id='dailyTrafficRows';
  for(const day of days){const row=el('tr');for(const value of [day.date,day.download_bytes==null?'缺失':bytes(day.download_bytes),day.upload_bytes==null?'缺失':bytes(day.upload_bytes),bytes(day.estimated_download_bytes)+' / '+bytes(day.estimated_upload_bytes),(day.observed_minutes||0)+' / '+(day.unknown_seconds||0)])row.append(el('td',value));body.append(row);}
  table.append(body);wrap.append(table);details.append(el('summary','查看每日数据'),wrap);panel.append(details);
}
async function loadDailyTraffic(){
  const selected=nodeId,previous=$('dailyTrafficPanel');
  if(previous&&previous.dataset.nodeId!==selected)previous.remove();
  const data=await api(`/nodes/${selected}/daily-traffic`);
  if(selected!==nodeId)return;
  renderDailyTraffic(data.days,data.days[data.days.length-1]);
}
function chartHover(chart,points,x,y,multiIp){
  if(!points.length)return;
  const host=$('chart'),tip=el('div',null,'fleet-chart-tooltip');
  tip.setAttribute('role','tooltip');tip.hidden=true;host.append(tip);
  const guide=svg('line',{y1:20,y2:230,class:'chart-guide',visibility:'hidden'});
  const markers=['actual','program'].map(kind=>svg('circle',{r:4,class:'chart-marker '+kind,visibility:'hidden'}));
  chart.append(guide,...markers);
  let selected=points.length-1;
  const hide=()=>{tip.hidden=true;guide.setAttribute('visibility','hidden');markers.forEach(marker=>marker.setAttribute('visibility','hidden'));};
  const show=index=>{
    selected=index;const point=points[index],xx=x(index);
    tip.replaceChildren(el('strong',stamp(point.start)+' – '+clock(point.end??point.start+600)),el('small','北京时间 · 10 分钟区间均速'));
    for(const [key,label,kind] of [['actual_mbps','SNMP 下行','actual'],['program_mbps',multiIp?'下载统计':'拉流进程','program']]){
      const value=point[key],row=el('div',null,'fleet-chart-tooltip-row');
      row.append(el('span',label,'chart-label-'+kind),el('b',value==null?(key==='actual_mbps'&&multiIp?'未接入':'缺失'):Number(value).toFixed(2)+' Mbps'));
      tip.append(row);
    }
    tip.append(el('small','未知 '+(point.unknown_seconds??'—')+' 秒 · 上报 '+(point.reporting_lines??'—')+' 条线路'));
    guide.setAttribute('x1',xx);guide.setAttribute('x2',xx);guide.setAttribute('visibility','visible');
    markers.forEach((marker,i)=>{const value=point[i===0?'actual_mbps':'program_mbps'];marker.setAttribute('visibility',value==null?'hidden':'visible');if(value!=null){marker.setAttribute('cx',xx);marker.setAttribute('cy',y(value));}});
    tip.hidden=false;
    const anchor=chart.createSVGPoint();anchor.x=xx;anchor.y=20;
    const screen=anchor.matrixTransform(chart.getScreenCTM()),bounds=host.getBoundingClientRect();
    const left=screen.x-bounds.left+12;
    tip.style.left=Math.max(0,Math.min(left,host.clientWidth-tip.offsetWidth))+'px';
    tip.style.top=Math.max(0,screen.y-bounds.top+8)+'px';
  };
  chart.addEventListener('pointermove',event=>{
    const cursor=chart.createSVGPoint();cursor.x=event.clientX;cursor.y=event.clientY;
    const at=cursor.matrixTransform(chart.getScreenCTM().inverse());
    if(at.x<59.5||at.x>980.5||at.y<19.5||at.y>230.5){hide();return;}
    show(Math.max(0,Math.min(points.length-1,Math.round((at.x-60)/920*(points.length-1)))));
  });
  chart.addEventListener('pointerleave',hide);
  chart.setAttribute('tabindex','0');
  chart.addEventListener('focus',()=>show(selected));chart.addEventListener('blur',hide);
  chart.addEventListener('keydown',event=>{
    if(event.key==='Escape'){hide();return;}
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    event.preventDefault();show(event.key==='Home'?0:event.key==='End'?points.length-1:Math.max(0,Math.min(points.length-1,selected+(event.key==='ArrowRight'?1:-1))));
  });
}
async function loadSeries(){const selected=nodeId,line=$('chartLine').value;const data=await api(`/nodes/${selected}/series`+(line?'?line_id='+encodeURIComponent(line):''));if(nodeId!==selected||line!==$('chartLine').value)return;const points=data.points;$('actualCurveLabel').textContent=current.snapshot.mode==='multi_ip'?'● SNMP 未接入':'● SNMP 下行';$('programCurveLabel').textContent=current.snapshot.mode==='multi_ip'?'● 下载统计':'● 拉流进程';const chart=svg('svg',{viewBox:'0 0 1000 280',role:'img','aria-label':current.snapshot.mode==='multi_ip'?'最近24小时下载统计折线，未接入SNMP，详细数值见下方表格':'最近24小时SNMP与拉流下载速度曲线，详细数值见下方表格'});const max=Math.max(1,...points.flatMap(p=>[p.actual_mbps||0,p.program_mbps||0]))*1.15,x=i=>60+i*920/Math.max(1,points.length-1),y=v=>230-v/max*210;for(let i=0;i<=4;i++){const yy=y(max*i/4);chart.append(svg('line',{x1:60,x2:980,y1:yy,y2:yy,class:'grid'}));const text=svg('text',{x:4,y:yy+4});text.textContent=(max*i/4).toFixed(0);chart.append(text);}for(let i=0;i<points.length;i+=24){const text=svg('text',{x:x(i),y:263});text.textContent=clock(points[i].start);chart.append(text);}for(const [key,cls] of [['actual_mbps','actual'],['program_mbps','program']]){let segment=[];const flush=()=>{if(segment.length)chart.append(svg('polyline',{points:segment.join(' '),class:cls}));segment=[];};points.forEach((p,i)=>{if(p[key]==null){flush();return;}segment.push(x(i)+','+y(p[key]));if(key==='actual_mbps'){const circle=svg('circle',{cx:x(i),cy:y(p[key]),r:3,class:'point'}),title=svg('title');title.textContent=`${stamp(p.start)} · ${p[key].toFixed(2)} Mbps · 未知 ${p.unknown_seconds}s · ${p.reporting_lines} 条线路`;circle.append(title);chart.append(circle);}});flush();}$('chart').replaceChildren(chart);chartHover(chart,points,x,y,current.snapshot.mode==='multi_ip');if(points.every(p=>p.actual_mbps==null&&p.program_mbps==null))$('chart').append(el('p','暂无已完成的10分钟采集区间。','muted'));$('chartRows').replaceChildren();for(const p of points){const row=el('tr');for(const value of [stamp(p.start),p.actual_mbps==null?(current.snapshot.mode==='multi_ip'?'未接入':'缺失'):p.actual_mbps.toFixed(2),p.program_mbps==null?'缺失':p.program_mbps.toFixed(2),p.unknown_seconds??'—',bytes(p.expected_bytes),p.reporting_lines??'—'])row.append(el('td',value));$('chartRows').append(row);}}
async function loadPeriods(){
  const selected=nodeId,day=$('periodDate').value,data=await api(`/nodes/${selected}/periods?day=${encodeURIComponent(day)}`);
  if(selected!==nodeId||day!==$('periodDate').value)return;$('periods').replaceChildren();
  const filter=$('chartLine').value;
  for(const p of data.periods.sort((a,b)=>a.start-b.start)){
    const rows=p.lines.filter(line=>!filter||line.line_id===filter),card=el('div',null,'period');
    const actual=rows.reduce((s,l)=>s+l.actual_bytes,0),cumulative=rows.reduce((s,l)=>s+l.cumulative_actual_bytes,0);
    const known=rows.length>0&&rows.every(l=>l.target_bytes!=null),target=known?rows.reduce((s,l)=>s+l.target_bytes,0):null;
    const achieved=rows.filter(l=>l.completion==='achieved').length;
    card.append(el('h3',phaseNames[p.phase]||p.phase),el('span',p.mode==='daily'?'每日预测':'旧模式','badge'),el('p',clock(p.start)+'–'+clock(p.end)+' · '+({pending:'尚未开始',active:'进行中',closed:'已结束'}[p.state]||p.state),'muted'),el('strong',bytes(actual)),el('p','区间实际下载'));
    const unavailable=p.phase==='upload_only'?'上传专用 · 不设下载目标':p.state==='pending'?'尚未开始':'';
    card.append(completionMeter(cumulative,target,p.state==='closed'?'期末累计完成度':'当前累计完成度',unavailable));
    if(!unavailable)card.append(el('p','累计实际 '+bytes(cumulative)+' / 期末累计目标 '+bytes(target),'muted'));
    card.append(el('p',p.phase==='upload_only'?'完成度不适用':p.state!=='closed'?'等待期末结算':`已达标 ${achieved} / ${rows.length} · 未达标 ${rows.filter(l=>l.completion==='unmet').length} · 目标未知 ${rows.filter(l=>l.completion==='unknown').length}`));
    if(filter&&rows.length)card.append(el('p','线路结果：'+completion[rows[0].completion]));
    if(p.evening_override?.length)card.append(el('p','当晚临时放行：'+p.evening_override.join('、'),'amber'));
    $('periods').append(card);
  }
  if(!data.periods.length)$('periods').append(el('p','此日期暂无采集的目标快照。历史实际下载可在速度曲线查看。','muted'));
}
function renderConfig(){if(formDirty)return;formVersion=current.snapshot.config_version;formBaseline={...current.snapshot.config};$('configFields').replaceChildren();for(const [key,value] of Object.entries(current.snapshot.config||{})){if(!Object.hasOwn(fieldLabels,key))continue;const label=el('label',fieldLabels[key]||key);let input;if(typeof value==='boolean'){input=el('select');for(const [v,text] of [['true','开启'],['false','关闭']]){const option=el('option',text);option.value=v;input.append(option);}}else{input=el('input');input.type='number';input.step=key==='target_ratio'?'0.1':'1';}input.name=key;input.value=String(value);input.required=true;label.append(input);$('configFields').append(label);}}
function renderSourceStatus(snapshot){
  const source=snapshot.source_list;$('sourcePanel').hidden=!source;if(!source)return;
  const states={applied:'已应用',pending:'等待线路确认',partial:'部分线路未应用',failed:'应用失败'},version=value=>value==null||value===0?'尚无版本':String(value);
  $('sourceState').textContent=(source.enabled?'远程签名源':'本地源')+' · '+(states[source.apply_state]||'状态未知')+' · '+source.source_count+' 个源'+(source.stale?' · 清单已过期，沿用最后可用源':'');
  $('sourceMeta').textContent='已接收 '+version(source.revision)+' · 已应用 '+version(source.active_revision)+(source.pending_revision?' · 待应用 '+version(source.pending_revision):'')+' · 最近成功 '+(source.last_success_at||'尚未获取')+' · 下次刷新 '+(source.next_refresh_at||'—');
  $('sourceErrors').replaceChildren();
  for(const line of source.lines||[])if(line.apply_state!=='applied')$('sourceErrors').append(el('p',line.line_id+' · '+(states[line.apply_state]||line.apply_state)+(line.error?' · '+line.error:''),line.apply_state==='pending'?'muted':'error'));
  const errors=[...new Set([source.apply_error,source.last_error].filter(Boolean))];for(const error of errors)$('sourceErrors').append(el('p',error,'error'));
}
function renderDiagnostics(snapshot){
  const diagnostics=snapshot.diagnostics||[];$('diagnosticsPanel').hidden=!diagnostics.length;$('diagnostics').replaceChildren();
  const reasons={insufficient_time:'剩余下载时间不足',insufficient_budget:'预算不足一个完整线路档位',source_throughput:'下载源吞吐不足',line_cooldown:'线路故障冷却中',snmp_unavailable:'SNMP 数据不可用',missing_profile:'线路档案未确认'};
  for(const item of diagnostics)$('diagnostics').append(el('p',(item.line_id?item.line_id+' · ':'')+(item.message||reasons[item.reason]||item.reason||'目标完成情况待确认'),'notice'));
}
async function loadCommands(){const selected=nodeId,data=await api(`/nodes/${selected}/commands`);if(selected!==nodeId)return;$('commands').replaceChildren();busy=data.commands.some(c=>['queued','dispatched'].includes(c.state));for(const c of data.commands){const row=el('div',null,'command');row.append(el('strong',({start:'启动下载',stop:'停止下载',update_schedule:'更新调度参数'}[c.action]||c.action)+' · '+commandStates[c.state]),el('p',stamp(c.created_at)+' · '+c.actor,'muted'));if(Object.keys(c.params).length)row.append(el('code',JSON.stringify(c.params)));if(c.result?.error)row.append(el('p',c.result.error,'error'));$('commands').append(row);}if(!data.commands.length)$('commands').append(el('p','暂无远程操作记录','muted'));setControlState();}
function setControlState(){for(const [action,b] of [['start',$('start')],['stop',$('stop')],['update_schedule',$('configForm').querySelector('button')]])b.disabled=busy||!current?.online||(Array.isArray(current?.snapshot.capabilities)&&!current.snapshot.capabilities.includes(action));}
async function detail(){
  const selected=nodeId,node=await api('/nodes/'+selected);if(selected!==nodeId)return;
  current=node;const s=node.snapshot,summary=s.summary||{},liveUpload=uploadView(s,node.online);
  $('nodeName').textContent=node.name;
  const rename=$('renameNode');rename.onclick=()=>window.dispatchEvent(new CustomEvent('fleet-rename-node',{detail:{id:node.id,name:node.name}}));
  $('nodeMeta').textContent=`${node.external_id||node.id} · 最后心跳 ${stamp(node.last_seen)} · 统计采集 ${stamp(s.observed_at)}`;
  $('nodeState').textContent=node.online?'在线':'离线 · 最后上报数据';$('nodeState').className='badge '+(node.online?'online':'offline');
  $('detailKpis').replaceChildren(kpi(s.mode==='multi_ip'?(s.stats_source==='interface_fallback'?'当日下载 · 接口统计':'当日下载 · 程序统计'):'当日实际下载 · SNMP',bytes(summary.actual_bytes)),kpi('预计下载目标 · '+targetDescription(s),bytes(summary.target_bytes)),kpi('各线路剩余目标',bytes(summary.remaining_bytes)),kpi('当日累计上传 · SNMP',bytes(summary.upload_bytes)),kpi(liveUpload.label,liveUpload.value),kpi(s.mode==='multi_ip'?'当日下载统计':'当日拉流进程下载',bytes(summary.program_bytes)));
  $('nodeCompletion').replaceChildren(completionMeter(summary.actual_bytes,summary.target_bytes,'当日下载目标完成度','',summary),el('p','实际 '+bytes(summary.actual_bytes)+' / 目标 '+bytes(summary.target_bytes)+' · '+('目标按 '+targetDescription(s)+' 计算'),'muted'));
  $('quality').textContent=`${s.mode==='multi_ip'?'经典模式 · 配置下载速度 × 时段时长':s.mode==='daily'?'每日预测目标':'旧模式当日上传 × 目标倍率'} · 实测 ${bytes(summary.measured_bytes)} · 估算 ${bytes(summary.estimated_bytes)} · 历史口径 ${bytes(summary.legacy_bytes)} · 未知 ${summary.unknown_seconds??'—'} 秒 · SNMP ${s.snmp_status||'等待采集'}。当前上传速度取最近有效 SNMP 采样；尚未采集、过期或异常时显示未知。${s.rolling?'滚动 '+s.rolling.days+' 天实际 '+bytes(s.rolling.actual_bytes)+'，目标 '+bytes(s.rolling.target_bytes)+'。':''}`;
  if(s.mode!=='multi_ip'&&s.measurement_started_at)$('quality').textContent+=' SNMP累计自 '+stamp(s.measurement_started_at)+' 开始采集；此前程序下载历史保留在曲线中。';
  if(s.mode==='multi_ip')$('quality').textContent='经典模式：下载量沿用原服务的'+(s.stats_source==='interface_fallback'?'接口统计':'程序统计')+'，目标为配置下载速度 × 时段时长；未接入 SNMP，上传量及速度显示未知。原服务没有持久流量历史，曲线仅展示接入后有采样的区间。';
  $('manualState').textContent=s.manual_enabled?'节点已允许下载，实际运行仍受分时调度和预算约束。':'节点已手动停止下载。';
  options($('chartLine'),(s.lines||[]).map(l=>[l.line_id,l.line_id]),'全部线路');
  options($('groupFilter'),[...new Set((s.lines||[]).map(l=>l.group_id))].map(g=>[g,g]),'全部分组');
  renderSourceStatus(s);renderDiagnostics(s);
  $('nodeControls').hidden=Array.isArray(s.capabilities)&&s.capabilities.length===0;renderLines();renderConfig();await Promise.all([loadSeries(),loadPeriods(),loadCommands(),loadDailyTraffic()]);
}
async function sendCommand(action,params={}){if(busy||!current?.online)return;const description=action==='update_schedule'?'确认修改以下调度参数？\n'+JSON.stringify(params,null,2):action==='start'?'确认允许此节点启动下载？仍遵循现有时段和预算限制。':'确认停止此节点所有拉流下载？';if(!window.confirm(description))return;busy=true;setControlState();try{await api(`/nodes/${nodeId}/commands`,{method:'POST',body:JSON.stringify({action,params,config_version:(action==='update_schedule'?formVersion:current.snapshot.config_version)||''})});formDirty=false;await loadCommands();}catch(e){fail(e);busy=false;setControlState();}}
async function refresh(){if(refreshing)return;refreshing=true;try{$('error').hidden=true;await(nodeId?detail():overview());$('freshness').textContent='页面更新 '+clock(Date.now()/1000);}catch(e){fail(e);$('freshness').textContent='连接异常';}finally{refreshing=false;if(refreshPending){refreshPending=false;refresh();}}}
function route(){nodeId=location.hash.slice(1);if(nodeId&&!/^[a-f0-9-]{36}$/i.test(nodeId)){fail(new Error('无效的节点标识'));return;}$('overview').hidden=!!nodeId;$('detail').hidden=!nodeId;current=null;formDirty=false;linePage=0;$('periodDate').value=dayNow();if(refreshing){refreshPending=true;}else{refresh();}}
$('refresh').onclick=refresh;$('back').onclick=()=>{location.hash='';};$('status').onchange=()=>{nodePage=0;refresh();};let searchTimer;$('search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{nodePage=0;refresh();},300);};$('prev').onclick=()=>{nodePage--;refresh();};$('next').onclick=()=>{nodePage++;refresh();};$('linePrev').onclick=()=>{linePage--;renderLines();};$('lineNext').onclick=()=>{linePage++;renderLines();};$('groupFilter').onchange=()=>{linePage=0;renderLines();};$('chartLine').onchange=()=>{Promise.all([loadSeries(),loadPeriods()]).catch(fail);};$('periodDate').onchange=()=>{loadPeriods().catch(fail);};$('start').onclick=()=>sendCommand('start');$('stop').onclick=()=>sendCommand('stop');$('configForm').oninput=()=>{formDirty=true;};$('configForm').onsubmit=e=>{e.preventDefault();const params={};for(const [key,value] of new FormData(e.target)){const parsed=Number(value);if(parsed!==formBaseline[key])params[key]=parsed;}if(!Object.keys(params).length){fail(new Error('没有参数变更'));return;}sendCommand('update_schedule',params);};window.addEventListener('fleet-node-renamed',()=>{refreshPending=true;if(!refreshing){refreshPending=false;refresh();}});window.addEventListener('hashchange',route);setInterval(()=>{if(!document.hidden)refresh();},15000);route();
