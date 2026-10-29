// Folder projection/navigation executes actual UI functions without MIDI.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const nodes=new Map();const element=id=>{if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:'',classList:{toggle(){}},removeAttribute(){}});return nodes.get(id)};
const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'token'})},history:{replaceState(){}},location:{pathname:'/'},setTimeout(){},clearTimeout(){},AbortSignal,Date,console});
const source=fs.readFileSync(path.join(__dirname,'../synfact-transfer/web/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf('function changeTab(')),context);
vm.runInContext(source.slice(source.indexOf('function folderRows('),source.indexOf('function fileArgs(')),context);
vm.runInContext(`
 state.folders=[{id:1,parent:0,name:'Drums'},{id:2,parent:1,name:'Acoustic'},{id:3,parent:0,name:'<Pads>'}];
 const entries=[{source:'high',id:7,name:'Kick',folder:2,key:'high:7'},{source:'high',id:8,name:'Root',key:'high:8'},{source:'stock',id:9,name:'Stock'}];
 this.root=folderRows(entries,state.folders,0);this.child=folderRows(entries,state.folders,2);
 this.path=folderPath(2);this.options=folderOptions(3);state.catalogueVersion=2;state.folder=2;folderControls();
`,context);
assert.deepEqual(JSON.parse(JSON.stringify(context.root)).map(e=>e.key),['folder:3','folder:1','high:8']);
assert.equal(context.child.length,1);assert.equal(context.child[0].id,7);assert.equal(context.path,'/Drums/Acoustic');
assert.ok(context.options.includes('&lt;Pads&gt;'));assert.equal(element('folder-path').textContent,'/Drums/Acoustic');
assert.equal(element('folder-enable').hidden,true);assert.equal(element('folder-up').disabled,false);
console.log('Folder UI projection, scoped samples, escaped names and control state passed.');

// A folder remembered from H must not redirect an import opened from S.
async function importDestinations(){
 vm.runInContext(source.slice(source.indexOf("$('add').onclick="),source.indexOf('async function importFiles(')),context);
 element('upload').files=[{name:'Kick.wav'}];
 vm.runInContext(`state.connected=true;state.category='samples';state.folder=2;state.rows=[];
  this.uploads=[];ask=async()=>({});data=async()=>'';refresh=()=>{};
  task=async(action,args)=>{uploads.push({action,args});};`,context);
 for(const bank of ['H','S']){
  vm.runInContext(`state.sampleBank='${bank}';`,context);
  await element('add').onclick();
 }
 const uploads=JSON.parse(JSON.stringify(context.uploads));
 assert.equal(uploads.length,2);assert.equal(uploads[0].args.folder,2);assert.equal(uploads[0].args.extended,true);
 assert.equal(uploads[1].args.folder,0);assert.equal(uploads[1].args.extended,false);
 console.log('Imports preserve H destination and ignore remembered folders in S.');
}
importDestinations().catch(e=>{console.error(e);process.exitCode=1});
