'use strict';
const $=id=>document.getElementById(id);
let token=document.querySelector('meta[name="session-token"]').content;
history.replaceState(null,'',location.pathname);
const state={connected:false,tab:'drop',category:'projects',bank:'A',sampleBank:'S',rows:[],jobs:[],generation:0};
const waits=new Map(),selected=new Set(),seenErrors=new Set();
const transferActions=new Set(['put','get','hz_get','hz_rm','clear','sample_put','machine_put','firmware_put','download_group']);
let scanning=false,paused=false,serviceOnline=false,modalResolve=null,importing=false,activeTransfer=null,pendingDrops=[];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const size=n=>n>=1048576?(n/1048576).toFixed(1)+' MB':n>=1024?(n/1024).toFixed(1)+' KB':n+' B';
function message(text){$('message').textContent=text;$('message').hidden=false;clearTimeout(message.timer);message.timer=setTimeout(()=>$('message').hidden=true,8000)}
async function api(path,body,retry=true){
 let r;
 try{r=await fetch('/api/'+path,{method:body?'POST':'GET',signal:AbortSignal.timeout(10000),headers:{'X-Session-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})})}
 catch(e){throw Error('Connection lost. Restart Synfact Transfer.')}
 const result=await r.json();
 if(r.status===403&&result.code==='session_expired'&&retry){
  const page=await fetch('/',{cache:'no-store'});
  const next=new DOMParser().parseFromString(await page.text(),'text/html').querySelector('meta[name="session-token"]')?.content;
  if(!next)throw Error('Session expired. Restart the application.');
  token=next;return api(path,body,false);
 }
 if(!r.ok)throw Error(result.error||'Operation failed.');return result;
}
function showTransfer(action,name){
 const titles={get:'Downloading',hz_get:'Downloading',download_group:'Downloading files',put:'Uploading',sample_put:'Uploading sample',machine_put:'Uploading mods',firmware_put:'Uploading firmware',clear:'Deleting',hz_rm:'Deleting sample'};
 $('transfer-title').textContent=titles[action]||'Transferring';$('transfer-file').textContent=name||'';
 $('transfer-progress').removeAttribute('value');$('transfer-detail').textContent='Preparing transfer…';
 $('transfer-cancel').disabled=false;$('transfer-cancel').textContent='Cancel';
 if(!$('transfer-dialog').open)$('transfer-dialog').showModal();
}
async function task(action,args={}){
 const modal=transferActions.has(action);
 if(modal)showTransfer(action,args.name||args.path||'');
 try{
  const r=await api('jobs',{action,...args});if(modal)activeTransfer=r.id;
  return await new Promise((resolve,reject)=>waits.set(r.id,{resolve,reject}));
 }finally{if(modal){activeTransfer=null;if($('transfer-dialog').open)$('transfer-dialog').close()}}
}
async function cancelTransfer(){if(!activeTransfer)return;$('transfer-cancel').disabled=true;$('transfer-cancel').textContent='Cancelling…';try{await api('cancel',{id:activeTransfer})}catch(e){message(e.message)}}
$('transfer-cancel').onclick=cancelTransfer;
$('transfer-dialog').oncancel=e=>{e.preventDefault();cancelTransfer()};
async function download(result){if(!result?.download)return;const r=await fetch('/api/download/'+result.download,{headers:{'X-Session-Token':token}});if(!r.ok)throw Error('Download failed.');const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download=result.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),60000)}
async function data(file){if(file.size>64*1024*1024)throw Error('File exceeds the 64 MB limit.');return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('Cannot read file.'));reader.readAsDataURL(file)})}
function ask(title,fields='',confirm='Continue'){if(modalResolve)modalResolve(null);$('dialog-title').textContent=title;$('dialog-fields').innerHTML=fields;$('dialog-confirm').textContent=confirm;$('dialog').showModal();return new Promise(resolve=>modalResolve=resolve)}
function closeDialog(value){$('dialog').close();if(modalResolve){const resolve=modalResolve;modalResolve=null;resolve(value)}}
$('dialog-form').onsubmit=e=>{e.preventDefault();closeDialog(Object.fromEntries(new FormData(e.target)))};
$('dialog-cancel').onclick=()=>closeDialog(null);$('dialog').oncancel=e=>{e.preventDefault();closeDialog(null)};
async function tick(){
 try{
  const s=await api('state'),changed=state.connected!==s.connected;serviceOnline=true;Object.assign(state,s);
  $('disconnect').hidden=!s.connected;$('resume').hidden=s.connected;
  $('device-dot').classList.toggle('online',s.connected);
  $('connect-status').textContent=s.connected?'Connected':paused?'Disconnected':'Connect your device';
  const active=s.jobs.find(j=>j.status==='running')||s.jobs.find(j=>j.status==='queued');
  $('activity').textContent=active?'Working…':s.connected?'Ready':'Waiting for device';
  for(const j of s.jobs){
   if(j.id===activeTransfer){
    const p=j.progress;
    if(p&&p.total>0){const percent=Math.min(100,Math.floor(p.current/p.total*100));$('transfer-progress').value=percent;$('transfer-detail').textContent=p.phase+' · '+percent+'% · '+size(p.current)+' / '+size(p.total)}
    else{$('transfer-progress').removeAttribute('value');$('transfer-detail').textContent=j.status==='queued'?'Waiting…':'Transferring…'}
   }
   const waiter=waits.get(j.id);
   if(waiter&&j.status==='done'){waits.delete(j.id);waiter.resolve(j.result)}
   else if(waiter&&['error','cancelled'].includes(j.status)){waits.delete(j.id);waiter.reject(Error(j.error||'Cancelled.'))}
   if(j.status==='error'&&!seenErrors.has(j.id)){seenErrors.add(j.id);message(j.error)}
  }
  if(changed&&s.connected){if(state.tab==='explore')refresh();if(pendingDrops.length){const files=pendingDrops;pendingDrops=[];dropFiles(files)}}
  if(changed&&!s.connected){state.rows=[];renderTable()}
 }catch(e){serviceOnline=false;state.connected=false;$('connect-status').textContent=e.message;for(const [id,w]of waits){w.reject(e);waits.delete(id)}}
 finally{setTimeout(tick,200)}
}
async function scan(){
 if(scanning||paused||!serviceOnline)return;scanning=true;
 try{
  const p=await api('ports');
  if(state.connected){if((state.input&&!p.inputs.includes(state.input))||(state.output&&!p.outputs.includes(state.output)))await task('disconnect')}
  else{
   // Match the supported USB MIDI endpoint without exposing manufacturer branding.
   const match=n=>/syntakt/i.test(n);const input=p.inputs.find(match),output=p.outputs.find(match);
   if(input&&output){$('connect-status').textContent='Connecting…';await task('connect',{input,output,profile:'auto'})}
  }
 }catch(e){message(e.message);paused=true;$('resume').hidden=false}
 finally{scanning=false}
}
$('resume').onclick=()=>{paused=false;scan()};
$('disconnect').onclick=async()=>{paused=true;try{await task('disconnect')}catch(e){message(e.message)}};
function changeTab(tab){state.tab=tab;$('drop-panel').hidden=tab!=='drop';$('explore-panel').hidden=tab!=='explore';$('drop-tab').classList.toggle('active',tab==='drop');$('explore-tab').classList.toggle('active',tab==='explore');if(tab==='explore')refresh()}
$('drop-tab').onclick=()=>changeTab('drop');$('explore-tab').onclick=()=>changeTab('explore');
async function dropFiles(files){
 if(files.some(file=>! /\.(syx|smz4)$/i.test(file.name))){message('Drop only accepts .syx and .smz4 files.');return}
 if(!state.connected){pendingDrops.push(...files);changeTab('drop');message(files.map(f=>f.name).join(', ')+' · File received. Connect your device to continue.');return}
 if(importing){message('Wait for the current transfer to finish.');return}
 importing=true;
 try{for(const file of files){
  try{
   const bytes=new Uint8Array(await file.slice(0,4).arrayBuffer());
   const header=String.fromCharCode(...bytes);
   const magic=bytes.length===4&&(header==='SMZ4'||header==='SAFE'||/^S[0-9]{1,2}Z[0-9]?$/.test(header));
   const machine=/\.smz4$/i.test(file.name);
   if(machine&&!magic)throw Error('Invalid mods file: unsupported mods header.');
   const ok=await ask(machine?'Upload mods':'Upload firmware','<p>'+esc(file.name)+'</p>','Upload');if(!ok)continue;
   const row=document.createElement('div');row.className='drop-file-result';const label=document.createElement('span'),status=document.createElement('span');label.textContent=file.name;status.textContent='Queued';row.append(label,status);$('drop-files').prepend(row);
   try{status.textContent='Transferring…';await task(machine?'machine_put':'firmware_put',{data:await data(file),name:file.name,confirm:true});status.textContent=machine?'Uploaded · Restart your device':'Uploaded'}
   catch(e){status.textContent='Failed';throw e}
  }catch(e){message(e.message)}
 }}finally{importing=false}
}
$('choose-drop').onclick=()=>$('drop-file').click();$('drop-file').onchange=e=>{dropFiles([...e.target.files]);e.target.value=''};
$('drop-zone').onkeydown=e=>{if(e.key==='Enter')$('drop-file').click()};
function bankButtons(){const area=$('banks'),samples=state.category==='samples',field=samples?'sampleBank':'bank';area.replaceChildren();for(const bank of samples?'SH':'ABCDEFGH'){const b=document.createElement('button');b.textContent=bank;b.classList.toggle('active',bank===state[field]);b.onclick=()=>{state[field]=bank;refresh()};area.append(b)}}
function path(){return state.category==='projects'?'/projects':'/soundbanks/'+state.bank}
function completeSlots(entries,count,base){const bySlot=new Map(entries.filter(e=>!e.directory).map(e=>[e.slot,e]));return Array.from({length:count},(_,i)=>{const slot=i+1,e=bySlot.get(slot);return {...e,slot,path:base+'/'+slot,key:base+'/'+slot,name:e?.valid?e.name:'',valid:!!e?.valid,bytes:e?.size||0}})}
$('category').onchange=e=>{state.category=e.target.value;refresh()};$('refresh').onclick=refresh;$('search').oninput=renderTable;
async function refresh(){
 const generation=++state.generation;selected.clear();state.rows=[];
 const samples=state.category==='samples';$('banks').hidden=state.category==='projects';bankButtons();$('download-selected').disabled=true;
 $('list-label').textContent=samples?(state.sampleBank==='S'?'Stock samples · S1–S64':'High memory samples'):state.category==='projects'?'128 projects':'Bank '+state.bank+' · 256 slots';
 $('table-head').innerHTML='';$('table-body').innerHTML='';$('empty-state').hidden=false;
 if(!state.connected){$('empty-state').textContent='Connect your device to browse files.';return}
 $('empty-state').textContent='Loading…';
 try{
  if(samples){const library=await task('sample_list');if(generation!==state.generation)return;
   const stock=new Map(library.entries.filter(e=>e.source==='stock').map(e=>[e.slot,e]));
   state.rows=state.sampleBank==='S'?Array.from({length:64},(_,i)=>{const e=stock.get(i+1);return {...e,source:'stock',slot:i+1,path:'/samples/'+(i+1),key:'stock:'+(i+1),name:e?.name||'',valid:!!e,bytes:e?.bytes||0}}):library.entries.filter(e=>e.source==='high').map(e=>({...e,valid:true}));
   if(state.sampleBank==='H'&&library.high_error)message('Extended sample storage is unavailable.');
  }else{const result=await task('list',{path:path()});if(generation!==state.generation)return;state.rows=completeSlots(result.entries,state.category==='projects'?128:256,path())}
  $('search').value='';renderTable();
 }catch(e){if(generation===state.generation){$('empty-state').hidden=false;$('empty-state').textContent=e.message}}
}
function renderTable(){
 const high=state.category==='samples'&&state.sampleBank==='H';
 const q=$('search').value.toLowerCase(),rows=state.rows.filter(e=>(e.name+' '+e.slot+' '+e.key).toLowerCase().includes(q));
 $('empty-state').hidden=rows.length>0;if(!rows.length)$('empty-state').textContent=state.connected?(q?'No results':'No files'):'Connect your device to browse files.';
 $('table-head').innerHTML='<tr><th class="select-cell"><input id="select-all" type="checkbox" aria-label="Select all"></th>'+(high?'':'<th>Slot</th>')+'<th>Name</th><th>Size</th><th></th></tr>';
 $('table-body').innerHTML=rows.map(e=>'<tr data-slot-key="'+esc(e.key)+'" class="'+(selected.has(e.key)?'selected':'')+'"><td class="select-cell"><input type="checkbox" aria-label="Select '+esc(e.name||e.slot)+'" data-select="'+esc(e.key)+'" '+(selected.has(e.key)?'checked':'')+' '+(!e.valid?'disabled':'')+'></td>'+(high?'':'<td class="number">'+(state.category==='samples'?'S'+e.slot:String(e.slot).padStart(3,'0'))+'</td>')+'<td class="'+(!e.valid?'empty-slot':'')+'">'+esc(e.name||'—')+'</td><td>'+(e.valid?size(e.bytes||0):'—')+'</td><td class="row-actions">'+(e.valid?'<button data-get="'+esc(e.key)+'">Download</button><button data-remove="'+esc(e.key)+'">Delete</button>':'')+'</td></tr>').join('');
 const all=$('select-all');all.checked=rows.some(e=>e.valid)&&rows.filter(e=>e.valid).every(e=>selected.has(e.key));all.onchange=()=>{for(const e of rows.filter(e=>e.valid))all.checked?selected.add(e.key):selected.delete(e.key);renderTable()};
 document.querySelectorAll('[data-select]').forEach(b=>b.onchange=()=>{b.checked?selected.add(b.dataset.select):selected.delete(b.dataset.select);updateSelection();b.closest('tr').classList.toggle('selected',b.checked)});
 document.querySelectorAll('[data-get]').forEach(b=>b.onclick=()=>getFile(state.rows.find(e=>e.key===b.dataset.get)).then(download).catch(e=>message(e.message)));
 document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>removeRow(state.rows.find(e=>e.key===b.dataset.remove)));updateSelection();
}
function updateSelection(){$('download-selected').disabled=!selected.size;$('download-selected').textContent=selected.size?'Download ('+selected.size+')':'Download'}
function fileArgs(e){return {path:e.path,id:e.id,source:e.source,name:e.name,bytes:e.bytes}}
function getFile(e){return task(e.source==='high'?'hz_get':'get',fileArgs(e))}
$('download-selected').onclick=async()=>{try{const rows=state.rows.filter(e=>selected.has(e.key)&&e.valid);await download(rows.length===1?await getFile(rows[0]):await task('download_group',{items:rows.map(fileArgs)}))}catch(e){message(e.message)}};
async function removeRow(e){const result=await ask('Delete file','<p>'+esc(e.name)+'</p>','Delete');if(!result)return;try{await task(e.source==='high'?'hz_rm':'clear',{...fileArgs(e),confirm:true});refresh()}catch(error){message(error.message)}}
$('add').onclick=async()=>{
 if(!state.connected){message('Connect your device first.');return}
 const sample=state.category==='samples',type=state.category,max=type==='projects'?128:256,base=sample?null:path(),free=state.rows.find(e=>!e.valid)?.slot||1;
 const result=await ask(sample?'Add samples':'Add file','<label>File<input id="upload" type="file" name="upload" accept="'+(sample?'.wav':type==='projects'?'.stprj':'.stsnd')+'" '+(sample?'multiple':'')+' required></label>'+(sample?'':'<label>Slot<input name="slot" type="number" min="1" max="'+max+'" value="'+free+'" required></label>'),'Upload');
 if(!result)return;const files=[...$('upload').files];try{const extension=sample?'.wav':type==='projects'?'.stprj':'.stsnd';if(files.some(file=>!file.name.toLowerCase().endsWith(extension)))throw Error('Choose '+extension+' files only.');for(const file of files)await task(sample?'sample_put':'put',{data:await data(file),name:file.name.replace(/\.[^.]+$/,''),...(sample?{}:{path:base+'/'+result.slot}),confirm:true});refresh()}catch(e){message(e.message)}
};
async function importFiles(files,targetKey){
 if(importing){message('Wait for the current transfer to finish.');return}if(!state.connected){message('Connect your device first.');return}importing=true;
 try{
  const sample=state.category==='samples',extension=sample?'.wav':state.category==='projects'?'.stprj':'.stsnd';
  if(files.some(f=>!f.name.toLowerCase().endsWith(extension)))throw Error('Choose '+extension+' files.');
  const target=state.rows.find(e=>e.key===targetKey),stock=sample&&target?.source==='stock',max=stock?64:state.category==='projects'?128:256;
  let first=target?.slot;
  if(!sample&&!first){for(let slot=1;slot<=max-files.length+1;slot++){if(files.every((_,i)=>!state.rows.find(e=>e.slot===slot+i)?.valid)){first=slot;break}}}
  if((!sample||stock)&&(!first||first+files.length-1>max))throw Error('Not enough consecutive slots.');
  const replace=(!sample||stock)&&files.some((_,i)=>state.rows.find(e=>e.slot===first+i&&(stock?e.source==='stock':true))?.valid);
  if(replace&&!await ask('Replace occupied slots?','<p>'+files.map(f=>esc(f.name)).join('<br>')+'</p>','Replace'))return;
  for(let i=0;i<files.length;i++){const file=files[i];await task(sample&&!stock?'sample_put':'put',{data:await data(file),name:file.name.replace(/\.[^.]+$/,''),...(sample&&!stock?{}:{path:(stock?'/samples':path())+'/'+(first+i)}),confirm:true})}
  message('Upload complete.');
 }catch(e){message(e.message)}finally{importing=false;refresh()}
}
function routeFiles(files,x,y){
 if($('transfer-dialog').open||$('dialog').open){message('Finish the current operation before dropping more files.');return}
 if(state.tab==='drop')return dropFiles(files);
 const target=document.elementFromPoint(x,y)?.closest('tr[data-slot-key]')?.dataset.slotKey;
 return importFiles(files,target);
}
window.receiveNativeFiles=(items,x,y)=>routeFiles(items.map(item=>{const bytes=Uint8Array.from(atob(item.data),c=>c.charCodeAt(0));return new File([bytes],item.name)}),x,y);
document.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();e.dataTransfer.dropEffect='copy';$('drop-zone').classList.add('drag')}});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)$('drop-zone').classList.remove('drag')});
document.addEventListener('drop',e=>{e.preventDefault();$('drop-zone').classList.remove('drag');routeFiles([...e.dataTransfer.files],e.clientX,e.clientY)});
bankButtons();tick();setInterval(scan,2000);
