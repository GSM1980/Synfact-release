// Read and verification speed remains visible with and without a known total.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const elements=new Map();
const element=id=>{
 if(!elements.has(id))elements.set(id,{hidden:false,textContent:'',classList:{toggle(){}},content:'token',removeAttribute(name){delete this[name]}});
 return elements.get(id);
};
const context=vm.createContext({document:{getElementById:element,querySelector:()=>element('token')},
 history:{replaceState(){}},location:{pathname:'/'},setTimeout(){},clearTimeout(){},AbortSignal,Date,console});
const source=fs.readFileSync(path.join(__dirname,'../synfact-transfer/web/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf('function changeTab(')),context);
(async()=>{
 await vm.runInContext(`
  activeTransfer='read';state.connected=true;
  api=async()=>({connected:true,jobs:[{id:'read',status:'running',progress:{phase:'Verifying',current:8192,total:16384,kib_s:128.25}}]});
  tick();
 `,context);
 assert.equal(element('transfer-progress').value,50);
 assert.match(element('transfer-detail').textContent,/Verifying.*50%.*128\.3 KiB\/s/);
 await vm.runInContext(`
  api=async()=>({connected:true,jobs:[{id:'read',status:'running',progress:{phase:'Downloading',current:8192,total:0,kib_s:140}}]});
  tick();
 `,context);
 assert.equal(element('transfer-progress').value,undefined);
 assert.match(element('transfer-detail').textContent,/Downloading.*8\.0 KB.*140\.0 KiB\/s/);
 await vm.runInContext(`
  api=async()=>({connected:true,jobs:[{id:'read',status:'running',progress:{phase:'Downloading',current:0,total:0,kib_s:0}}]});
  tick();
 `,context);
 assert.equal(element('transfer-detail').textContent,'Downloading · 0 B');
 for(const phase of ['Checking sample library','Checking sample storage','Updating sample library','Verifying sample library','Verifying sample (full read)']){
  await vm.runInContext(`
   api=async()=>({connected:true,jobs:[{id:'read',status:'running',progress:{phase:${JSON.stringify(phase)},current:0,total:0,kib_s:0}}]});
   tick();
  `,context);
  assert.equal(element('transfer-detail').textContent,phase+' · 0 B');
 }
 await vm.runInContext(`
  api=async()=>({connected:true,jobs:[{id:'read',status:'running',progress:{phase:'Verifying sample',current:192,total:192,kib_s:10}}]});
  tick();
 `,context);
 assert.equal(element('transfer-progress').value,100);
 assert.match(element('transfer-detail').textContent,/Verifying sample.*100%.*192 B \/ 192 B/);
 await vm.runInContext(`
  api=async()=>({connected:true,jobs:[{id:'read',status:'running',progress:{phase:'Verifying',current:2048,total:524331,kib_s:140}}]});
  tick();
 `,context);
 assert.equal(element('transfer-progress').value,0);
 assert.match(element('transfer-detail').textContent,/Verifying.*0%/);
 console.log('Transfer progress UI checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
