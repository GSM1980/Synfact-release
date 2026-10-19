// Connection scheduling regressions, using a minimal DOM and no device access.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const elements=new Map();
const element=id=>{
 if(!elements.has(id))elements.set(id,{hidden:false,textContent:'',classList:{toggle(){}},content:'token'});
 return elements.get(id);
};
const context=vm.createContext({
 document:{getElementById:element,querySelector:()=>element('token')},
 history:{replaceState(){}},location:{pathname:'/'},
 setTimeout(){},clearTimeout(){},AbortSignal,Date,console,
});
const source=fs.readFileSync(path.join(__dirname,'../synfact-transfer/web/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf('function changeTab(')),context);
async function run(script){return vm.runInContext(script,context)}
(async()=>{
 await run(`
  serviceOnline=true;
  let calls=[],failConnect=true;
  api=async()=>({inputs:['Syntakt'],outputs:['Syntakt']});
  task=async action=>{calls.push(action);if(action==='connect'&&failConnect)throw Error('USB reset')};
 `);
 await run('scan()');
 assert.equal(await run('paused'),false,'Failed connect must not disable automatic discovery');
 assert.equal(await run('scanning'),false,'Failed connect must release the scan guard');
 assert.equal(await run('nextScan>Date.now()'),true,'Retries must back off');
 await run('nextScan=0;failConnect=false;scan()');
 assert.equal(await run('calls.join(",")'),'connect,connect');
 assert.equal(await run('connectionFailures'),0);
 await run("state.connected=true;state.input='Syntakt';state.output='Syntakt';api=async()=>({inputs:[],outputs:[]});scan()");
 assert.equal(await run('calls.at(-1)'),'disconnect','USB disappearance must interrupt the connection');
 await run('paused=true;calls=[];scan()');
 assert.equal(await run('calls.length'),0,'Explicit disconnect must remain paused');
 await run(`
  renderTable=()=>{};
  state.connected=true;state.generation=5;state.rows=[{name:'old'}];selected.add('old');
  api=async()=>({connected:false,jobs:[]});
  tick();
 `);
 assert.equal(await run('state.generation'),6,'Connection loss must invalidate pending listings');
 assert.equal(await run('state.rows.length'),0);
 assert.equal(await run('selected.size'),0);
 console.log('Connection UI checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
