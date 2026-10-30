import {deviceCrc,validateObject} from './native-engine.mjs';
const view=a=>new DataView(a.buffer,a.byteOffset,a.byteLength);
const ascii=(a,p,n)=>String.fromCharCode(...a.subarray(p,p+n));
function roundEven(x){const n=Math.floor(x),fraction=x-n;return fraction===0.5?(n%2===0?n:n+1):Math.round(x);}
export function readWav(bytes){
 const v=view(bytes);if(bytes.length<12||ascii(bytes,0,4)!=='RIFF'||ascii(bytes,8,4)!=='WAVE'||v.getUint32(4,true)+8>bytes.length)throw Error('Invalid WAV file.');
 let fmt,pcm;const end=v.getUint32(4,true)+8;for(let p=12;p+8<=end;){const id=ascii(bytes,p,4),size=v.getUint32(p+4,true),start=p+8;if(start+size>end)throw Error('Truncated WAV chunk.');if(id==='fmt ')fmt=bytes.subarray(start,start+size);if(id==='data')pcm=bytes.subarray(start,start+size);p=start+size+(size&1);}
 if(!fmt||fmt.length<16||!pcm)throw Error('WAV is missing fmt or data.');
 const f=view(fmt),channels=f.getUint16(2,true),rate=f.getUint32(4,true),bits=f.getUint16(14,true),tag=f.getUint16(0,true)===0xfffe?(fmt.length>=26?f.getUint16(24,true):0):f.getUint16(0,true),width=bits/8;
 if(!channels||channels>32||!rate||rate>48000||!Number.isInteger(width)||!((tag===1&&[8,16,24,32].includes(bits))||(tag===3&&bits===32)))throw Error('Unsupported WAV format or sample rate above 48 kHz.');
 const frames=Math.floor(pcm.length/(width*channels)),outLength=Math.floor(frames*48000/rate);if(outLength>240000)throw Error('Stock samples support up to 5 seconds.');
 const pv=view(pcm),mono=new Float64Array(frames);for(let i=0;i<frames;i++){let sum=0;for(let c=0;c<channels;c++){const p=(i*channels+c)*width;let value;if(tag===3)value=pv.getFloat32(p,true);else if(bits===8)value=(pcm[p]-128)/128;else if(bits===16)value=pv.getInt16(p,true)/32768;else if(bits===24){let x=pcm[p]|pcm[p+1]<<8|pcm[p+2]<<16;if(x&0x800000)x|=0xff000000;value=x/8388608;}else value=pv.getInt32(p,true)/2147483648;if(!Number.isFinite(value))throw Error('Invalid WAV sample.');sum+=value;}mono[i]=sum/channels;}
 const output=new Uint8Array(outLength*2),out=view(output);for(let i=0;i<outLength;i++){let x;if(rate===48000)x=mono[i];else{const pos=i*rate/48000,j=Math.floor(pos),t=pos-j,y0=mono[j?j-1:0],y1=mono[j],y2=mono[Math.min(j+1,frames-1)],y3=mono[Math.min(j+2,frames-1)],c1=.5*(y2-y0),c2=y0-2.5*y1+2*y2-.5*y3,c3=.5*(y3-y0)+1.5*(y1-y2);x=((c3*t+c2)*t+c1)*t+y1;}out.setInt16(i*2,Math.max(-32768,Math.min(32767,roundEven(x*32768))));}
 return output;
}
function labelBytes(name){const result=[];const decoder=new TextDecoder('windows-1252');const mapping=new Map(Array.from({length:256},(_,i)=>[decoder.decode(Uint8Array.of(i)),i]));for(const c of name){const b=mapping.get(c);if(b===undefined||b===0)throw Error('Sample name must use Windows-1252 characters.');result.push(b);}if(!result.length||result.length>16)throw Error('Sample name must contain 1–16 bytes.');return Uint8Array.from(result);}
export function buildSample(pcm,slot,name){
 if(slot<1||slot>64||pcm.length%2||pcm.length>480000)throw Error('Invalid stock sample.');const label=labelBytes(name),obj=new Uint8Array(31+64+pcm.length+12),v=view(obj),body=obj.subarray(31,-12);v.setUint32(0,0xac11d303);obj[4]=2;v.setUint16(5,8);v.setUint16(7,13);obj.set([48,48,56,50],9);v.setUint32(13,8);v.setUint32(17,0xffffffff);v.setUint32(21,slot);v.setUint32(25,body.length);obj[30]=12;v.setUint32(31,0x53414d50);v.setUint32(39,pcm.length/2);obj.set(label,43);obj.set(pcm,95);v.setUint32(obj.length-12,deviceCrc(body));v.setUint32(obj.length-8,body.length);v.setUint32(obj.length-4,0xaaa1daaa);return obj;
}
export function sampleWav(obj){
 validateObject(obj);if(obj[29]||obj.length<107||view(obj).getUint32(31)!==0x53414d50)throw Error('Invalid sample object.');const pcm=obj.subarray(95,-12);if(pcm.length%2||view(obj).getUint32(39)*2!==pcm.length)throw Error('Invalid sample length.');
 const out=new Uint8Array(44+pcm.length),v=view(out);out.set(new TextEncoder().encode('RIFF'),0);v.setUint32(4,36+pcm.length,true);out.set(new TextEncoder().encode('WAVEfmt '),8);v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,48000,true);v.setUint32(28,96000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);out.set(new TextEncoder().encode('data'),36);v.setUint32(40,pcm.length,true);for(let i=0;i<pcm.length;i+=2){out[44+i]=pcm[i+1];out[45+i]=pcm[i];}return out;
}
