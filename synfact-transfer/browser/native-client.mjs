import {MidiConnection} from './midi.mjs';
import {listDirectory,readObject,packageObject,readPackage,writeObject,organize} from './native-engine.mjs';
import {convertDigitone} from './digitone.mjs';
import {readWav,buildSample,sampleWav} from './samples.mjs';
import {downloadCollection} from './collection.mjs';
import {uploadMods,uploadFirmware} from './drop.mjs';

let connection, status={connected:false,mode:'disconnected',profile:'stock',identity:'',input:'',output:''};
let nextId=1, current=null;
const jobs=[],downloads=new Map();
function reportProgress(){let started=performance.now(),lastPhase='';return(n,total,phase)=>{if(phase!==lastPhase||n===0){started=performance.now();lastPhase=phase;}const elapsed=(performance.now()-started)/1000;if(current)current.progress={current:n,total,phase,kib_s:elapsed>0?n/1024/elapsed:0};};}
function schedule(){
  if(current)return;
  current=jobs.find(job=>job.status==='queued');
  if(!current)return;
  const job=current;job.status='running';
  execute(job.action,job.args).then(result=>{if(job.cancel){job.status='cancelled';job.error='Cancelled. Check the device status.';}else{job.result=result;job.status='done';}},error=>{
    job.error=String(error.message||error);job.status=job.cancel?'cancelled':'error';
    if(connection&&(error.name==='MidiTimeoutError'||!connection.rpc||connection.rpc.closed)){connection.close();status={...status,connected:false,input:'',output:''};cancelQueued();}
  }).finally(()=>{current=null;schedule();});
}
function cancelQueued(){for(const job of jobs)if(job.status==='queued'){job.status='cancelled';job.error='Connection closed.';}}
async function execute(action,args){
  if(action==='connect'){
    if(!connection)throw Error('Select Connect to grant MIDI permission.');
    const ports=connection.ports(), input=ports.inputs.find(p=>p.name===args.input), output=ports.outputs.find(p=>p.name===args.output);
    if(!input||!output)throw Error('Syntakt MIDI ports unavailable.');
    await connection.open(input.id,output.id);
    try {
      const version=await connection.rpc.call(2);
      const text=new TextDecoder('windows-1252').decode(version.subarray(5)).split('\0')[0];
      status={connected:true,mode:'device',profile:'stock',identity:'Device · '+text,input:args.input,output:args.output};
      return status;
    }catch(error){connection.close();throw error;}
  }
  if(action==='disconnect'){connection?.close();status={connected:false,mode:'disconnected',profile:'stock',identity:'',input:'',output:''};return status;}
  if(!status.connected||!connection?.rpc)throw Error('Connect your device first.');
  if(action==='list')return listDirectory(connection.rpc,args.path);
  if(action==='machine_put'||action==='firmware_put'){
    if(args.confirm!==true)throw Error('Confirm the device modification.');
    const data=Uint8Array.from(atob(args.data),c=>c.charCodeAt(0));
    if(data.length>64*1024*1024)throw Error('File exceeds 64 MB.');
    return action==='machine_put'?uploadMods(connection.rpc,data,reportProgress()):uploadFirmware(connection.rpc,data,reportProgress());
  }
  if(action==='download_group'){
    const job=current,blob=await downloadCollection(connection.rpc,args.items,{progress:reportProgress(),cancelled:()=>job.cancel||!status.connected});
    if(job.cancel||!status.connected)throw Error('Download cancelled.');
    const key=String(nextId++);downloads.set(key,blob);return {download:key,filename:'Synfact-backup.zip'};
  }
  if(['clear','copy','move','swap','rename'].includes(action))return organize(connection.rpc,action,args);
  if(action==='get' && /^\/samples\//.test(args.path)){
    const obj=await readObject(connection.rpc,args.path,reportProgress());
    const name=String(args.name||'sample').replace(/[^\w. -]/g,'_').slice(0,100)||'sample',key=String(nextId++);
    downloads.set(key,new Blob([sampleWav(obj)],{type:'audio/wav'}));return {download:key,filename:name+'.wav'};
  }
  if(action==='sample_put'||(action==='put'&&/^\/samples\//.test(args.path||''))){
    if(args.confirm!==true)throw Error('Confirm the device modification.');
    if(args.extended||args.folder)throw Error('Extended storage is not yet available.');
    const pcm=readWav(Uint8Array.from(atob(args.data),c=>c.charCodeAt(0)));let slot;
    if(action==='sample_put'){const listed=await listDirectory(connection.rpc,'/samples'),used=new Set(listed.entries.filter(e=>e.valid&&!e.directory).map(e=>e.slot));slot=Array.from({length:64},(_,i)=>i+1).find(n=>!used.has(n));if(!slot)throw Error('No free stock sample slots.');}
    else {const m=/^\/samples\/(\d+)$/.exec(args.path);slot=m?Number(m[1]):0;}
    const obj=buildSample(pcm,slot,args.name||'SAMPLE');
    return writeObject(connection.rpc,'/samples/'+slot,obj,(n,total,phase)=>{if(current)current.progress={current:n,total,phase,kib_s:0};});
  }
  if(action==='get' && /^\/(projects|soundbanks\/)/.test(args.path)){
    const object=await readObject(connection.rpc,args.path,reportProgress());
    const project=args.path.startsWith('/projects/'),name=String(args.name||'backup').replace(/[^\w. -]/g,'_').slice(0,100)||'backup';
    const key=String(nextId++);downloads.set(key,new Blob([packageObject(object,project?'Project':'Sound')],{type:'application/zip'}));
    return {download:key,filename:name+(project?'.stprj':'.stsnd')};
  }
  if(action==='put' && /^\/(projects|soundbanks\/)/.test(args.path)){
    if(args.confirm!==true)throw Error('Confirm the device modification.');
    const bytes=Uint8Array.from(atob(args.data),c=>c.charCodeAt(0));
    let obj=await readPackage(bytes,args.path.startsWith('/projects/')?'Project':'Sound',{digitone:/\.dnsnd$/i.test(args.filename||'')});
    let notes=[];if(obj[8]===9){const converted=convertDigitone(obj,Number(args.path.split('/').at(-1))-1);obj=converted.object;notes=converted.notes;}
    const result=await writeObject(connection.rpc,args.path,obj,(n,total,phase)=>{if(current)current.progress={current:n,total,phase,kib_s:0};});
    return {...result,notes};
  }
  if(action==='sample_list'){
    const listed=await listDirectory(connection.rpc,'/samples');
    const entries=listed.entries.filter(e=>e.valid&&!e.directory&&e.slot>=1&&e.slot<=64).map(e=>({...e,source:'stock',key:'stock:'+e.slot,bytes:e.size,frames:null}));
    return {entries,stock_used:entries.length,stock_total:64,high_available:false,high_error:'Not ported yet',free:null,used:null,total:null,sequence:null,folders:[],catalogue_version:1};
  }
  throw Error('This operation has not yet been ported to the browser.');
}
globalThis.browserService={
 readOnly:true,
 projectDownload:true,
 projectUpload:true,
 groupDownload:true,
 stockSamples:true,
 organize:true,
 dropTransfers:true,
 async download(key){const result=downloads.get(key);if(!result)throw Error('Download expired.');downloads.delete(key);return result;},
 async requestMidi(){
   if(connection)connection.dispose();
   connection=await MidiConnection.request();
   connection.access.addEventListener('statechange',()=>{
     if(status.connected&&!connection.rpc){status={...status,connected:false,input:'',output:''};cancelQueued();}
   });
 },
 async api(path,body){
   if(path==='state')return {...status,jobs:jobs.map(({args,...job})=>job)};
   if(path==='ports'){const p=connection?.ports();return {inputs:p?.inputs.map(x=>x.name)||[],outputs:p?.outputs.map(x=>x.name)||[]};}
   if(path==='cancel'){const j=jobs.find(x=>x.id===body.id);if(j){j.cancel=true;if(j===current){connection?.close();status.connected=false;cancelQueued();}else j.status='cancelled';}return {ok:true};}
   if(path==='jobs'){
     const {action,...args}=body;
     if(action==='disconnect'){cancelQueued();if(current){current.cancel=true;connection?.close();}}
     const job={id:String(nextId++),action,args,status:'queued',logs:[]};jobs.push(job);schedule();return {id:job.id};
   }
   throw Error('Unknown browser operation.');
 },
};
const notice=document.getElementById('preview-notice');notice.hidden=false;
notice.textContent='Native Web MIDI preview · Project, sound and stock sample transfers are experimental. Extended sample storage is not yet available.';
const script=document.createElement('script');script.src='/web/app.js';document.head.append(script);
