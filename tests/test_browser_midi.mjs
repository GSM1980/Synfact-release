import {test} from 'node:test';
import assert from 'node:assert/strict';
import {encode87, decode87, MidiRpc, MidiConnection} from '../synfact-transfer/browser/midi.mjs';

test('8-in-7 codec matches the Syntakt framing', () => {
  const raw = Uint8Array.of(0x80, 0, 0xff, 0x7f, 0x81, 0x55, 0x80, 0xff);
  assert.deepEqual([...encode87(raw)], [0x55, 0, 0, 0x7f, 0x7f, 1, 0x55, 0, 0x40, 0x7f]);
  assert.deepEqual(decode87(encode87(raw)), raw);
});

class Input extends EventTarget {
  receive(data) { const e = new Event('midimessage'); e.data = data; this.dispatchEvent(e); }
}
function reply(seq, type) {
  return Uint8Array.of(0xf0, 0, 0x20, 0x3c, 0x10, 0, ...encode87(Uint8Array.of(0, 1, seq >> 8, seq & 255, type | 0x80, 1)), 0xf7);
}
test('RPC sends framed messages and matches the response sequence', async () => {
  const input = new Input(); const sent = [];
  const rpc = new MidiRpc(input, {send: frame => sent.push(frame)});
  const call = rpc.call(1, Uint8Array.of(0x80));
  assert.deepEqual([...decode87(sent[0].subarray(6, -1))], [0, 1, 0, 0, 1, 0x80]);
  input.receive(reply(2, 1));
  input.receive(reply(1, 1));
  assert.deepEqual([...await call], [0, 1, 0, 1, 0x81, 1]);
  rpc.close();
});
test('RPC rejects overlapping calls and cancels on close', async () => {
  const rpc = new MidiRpc(new Input(), {send() {}});
  const first = rpc.call(1);
  await assert.rejects(rpc.call(2), /already in progress/);
  rpc.close();
  await assert.rejects(first, /closed/);
});

test('connection requests SysEx and validates the device identity', async () => {
  const input = new Input(); Object.assign(input, {id:'in', name:'Syntakt', state:'connected', async open(){}, async close(){}});
  const output = {id:'out', name:'Syntakt', state:'connected', async open(){}, async close(){}, send(frame){
    const req = decode87(frame.subarray(6, -1));
    const data = Uint8Array.of(0, 1, req[0], req[1], 0x81, 1, 0, ...new TextEncoder().encode('Syntakt'), 0);
    queueMicrotask(() => input.receive(Uint8Array.of(0xf0, 0, 0x20, 0x3c, 0x10, 0, ...encode87(data), 0xf7)));
  }};
  const access = new EventTarget(); access.inputs = new Map([['in', input]]); access.outputs = new Map([['out', output]]);
  const conn = await MidiConnection.request({requestMIDIAccess: async options => { assert.equal(options.sysex, true); return access; }});
  assert.equal(await conn.open('in', 'out'), 'Syntakt');
  input.state = 'disconnected'; access.dispatchEvent(new Event('statechange'));
  assert.equal(conn.rpc, null);
  conn.dispose();
});
