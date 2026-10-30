// SPDX-License-Identifier: MIT
// Port of vendor/elekloader_digitone.py, Copyright (c) 2026 GSM1980 and contributors.
import {deviceCrc,objectRecord,validateObject} from './native-engine.mjs';
const range=(a,b)=>Array.from({length:b-a},(_,i)=>a+i);
const DN=[0,1,9,2,10,3,11,4,12,5,13,6,14,7,15,8,16,...range(17,50),...range(54,64),...range(66,77),78,77,50,51,52,53,64,65];
const ST=[0,1,9,2,10,3,11,4,12,5,13,6,14,7,15,8,16,...range(17,36),...range(37,52),53,52,36,...range(54,71)];
const source=[17,18,19,21,22,23,24,...range(27,38),25,38,39,40,55,56,57,58,59,60,61,62,63,64,66,67,68,69,71,72,74,73,76,1,2,3,5,6,7,8,9,10,11,13,14,15,16,77,78,20,50,51,52,53];
const dest=[18,19,20,22,23,26,25,...range(54,65),65,66,67,68,28,29,30,32,33,34,35,37,38,31,40,42,43,44,47,48,45,46,51,1,2,3,5,6,7,8,9,10,11,13,14,15,16,52,53,21,69,17,24,70];
const map=new Map(source.map((s,i)=>[s,dest[i]]));
const read32=(a,p)=>new DataView(a.buffer,a.byteOffset,a.byteLength).getUint32(p);
export function convertDigitone(obj,index=0){
 validateObject(obj,9);const record=objectRecord(obj,9),version=record.length>=8?read32(record,4):0;
 const sizes={1:0x11b,2:0x11a,3:0x11a,4:0x12e,5:0x12e,6:0x12e},size=sizes[version];
 if(!size||read32(record,0)!==0xbeefbace||![size,...(version===2?[0x12e]:[])].includes(record.length)||read32(record,size-4)!==0xbacef00c||read32(obj,13)!==3||read32(obj,17)!==version)throw Error('Invalid Digitone sound record.');
 const view=new DataView(record.buffer,record.byteOffset,record.byteLength),params=Array.from({length:80},(_,i)=>view.getUint16(28+2*i));
 if(version===1){params[71]=0x3f00;params[72]=0x2700;}
 if(version<=2){params[0]=0;for(let i=73;i<77;i++)params[i]=0x4000;}
 if(version<=4){params[0]=0;params[77]=0;}
 if(version<=5)for(const [wave,d] of [[9,11],[10,12]])if(params[wave]===6)params[d]=0;
 const words=new Map(DN.map((slot,i)=>[slot,params[i]]));
 for(const slot of [4,12]){const target=words.get(slot)>>8;words.set(slot,(DN[target]??0)<<8);}
 if(words.get(54)>0x3ff)words.set(54,0x300);
 const slots=new Map([[0,0],[27,0],[36,0],[39,0],[41,0],[49,0x100],[50,0x100]]);
 for(const [s,d] of map)slots.set(d,words.get(s));
 slots.set(21,Math.min(words.get(20)>>1,360)*88);const originalAttack=slots.get(40);slots.set(40,Math.max(originalAttack,0x800));slots.set(27,[7,0,1,0][words.get(54)>>8]<<8);
 const notes=['Digitone sound converted to Elekloader FM TONE. Chorus and arpeggiator data ignored.'];
 if(originalAttack<0x800)notes.push('AMP ATK raised to the minimum of 8.');
 if(words.get(54)>>8===3)notes.push('Filter LP4 imported as LP (Syntakt has no 4-pole lowpass).');
 for(const slot of [4,12]){const target=words.get(slot)>>8,d=map.get(target)??0;slots.set(slot,ST.indexOf(d)<<8);if(target&&!map.has(target))notes.push(`LFO${slot===4?1:2} destination remapped to none.`);}
 if(words.get(70))notes.push('AMP DRIVE not converted.');
 notes.push('Velocity/controller assignments are not imported; filter and effects may sound different.');
 const out=new Uint8Array(263),o=new DataView(out.buffer);o.setUint32(0,0xbeefbace);o.setUint32(4,7);o.setUint32(8,read32(record,8)&0x0fffffff);
 let text='';for(const c of record.subarray(12,28)){if(!c)break;const upper=String.fromCharCode(c).toUpperCase();for(const ch of upper)text+=ch.charCodeAt(0)>=32&&ch.charCodeAt(0)<127?ch:' ';}
 text=text.trim()||'DN SOUND';out.set(new TextEncoder().encode(text.slice(0,15)),12);
 ST.forEach((slot,i)=>o.setUint16(28+2*i,slots.get(slot)||0));out.set([65,2,0,0,1,2],172);out[252]=2;out[254]=100;o.setUint32(259,0xbacef00c);
 const lit=[];let remaining=out.length-15;lit.push(0xf0);while(remaining>=255){lit.push(255);remaining-=255;}lit.push(remaining);
 const block=Uint8Array.from([...lit,...out]),body=new Uint8Array(4+block.length+4),bv=new DataView(body.buffer);bv.setUint32(0,block.length);body.set(block,4);
 const result=new Uint8Array(31+body.length+12),v=new DataView(result.buffer);v.setUint32(0,0xac11d303);result[4]=2;v.setUint16(5,8);v.setUint16(7,13);result.set(new TextEncoder().encode('0044'),9);v.setUint32(13,3);v.setUint32(17,7);v.setUint32(21,0x700+(index&0x7f));v.setUint32(25,out.length);result[29]=1;result[30]=12;result.set(body,31);v.setUint32(31+body.length,deviceCrc(body));v.setUint32(35+body.length,body.length);v.setUint32(39+body.length,0xaaa1daaa);
 return {object:result,notes};
}
