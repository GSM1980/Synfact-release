// Browser implementation of the Syntakt SysEx RPC transport.
const HEADER = Uint8Array.of(0xf0, 0x00, 0x20, 0x3c, 0x10, 0x00);

export function encode87(raw) {
  const out = [];
  for (let i = 0; i < raw.length; i += 7) {
    const group = raw.subarray(i, i + 7);
    let high = 0;
    for (let j = 0; j < group.length; j++) if (group[j] & 0x80) high |= 0x40 >> j;
    out.push(high, ...Array.from(group, byte => byte & 0x7f));
  }
  return Uint8Array.from(out);
}

export function decode87(packed) {
  const out = [];
  for (let i = 0; i < packed.length; i += 8) {
    const high = packed[i];
    for (let j = 1; j < 8 && i + j < packed.length; j++)
      out.push((packed[i + j] & 0x7f) | ((high & (0x40 >> (j - 1))) ? 0x80 : 0));
  }
  return Uint8Array.from(out);
}

export class MidiRpc {
  constructor(input, output) {
    this.input = input; this.output = output; this.seq = 1; this.pending = null; this.closed = false;
    this.onmessage = event => this.receive(event.data);
    input.addEventListener('midimessage', this.onmessage);
  }
  receive(frame) {
    const pending = this.pending;
    if (!pending || frame.length < 8 || frame[frame.length - 1] !== 0xf7 ||
        !HEADER.every((byte, i) => frame[i] === byte)) return;
    const rx = decode87(frame.subarray(6, -1));
    if (rx.length < 6 || ((rx[2] << 8) | rx[3]) !== pending.seq) return;
    if (rx[4] !== (pending.type | 0x80)) return this.finish(new Error('Unexpected MIDI response type.'));
    this.finish(null, rx);
  }
  finish(error, value) {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null; clearTimeout(pending.timer);
    if (error) pending.reject(error); else pending.resolve(value);
  }
  call(type, payload = new Uint8Array(), timeout = 5000) {
    if (this.closed) return Promise.reject(new Error('MIDI connection closed.'));
    if (this.pending) return Promise.reject(new Error('MIDI RPC already in progress.'));
    const seq = this.seq; this.seq = ((seq + 1) & 0x3fff) || 1;
    const message = new Uint8Array(5 + payload.length);
    message.set([seq >> 8, seq & 255, 0, 0, type]); message.set(payload, 5);
    const encoded = encode87(message);
    const frame = new Uint8Array(HEADER.length + encoded.length + 1);
    frame.set(HEADER); frame.set(encoded, HEADER.length); frame[frame.length - 1] = 0xf7;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {const error=new Error('The device did not respond.');error.name='MidiTimeoutError';this.finish(error);}, timeout);
      this.pending = {seq, type, resolve, reject, timer};
      try { this.output.send(frame); } catch (error) { this.finish(error); }
    });
  }
  close() {
    this.closed = true;
    this.finish(new Error('MIDI connection closed.'));
    this.input.removeEventListener('midimessage', this.onmessage);
  }
}

// Keep permission requests under an explicit user gesture. Matching by both
// port names is only a convenience: the handshake still identifies Syntakt.
export class MidiConnection {
  constructor(access) {
    this.access = access;
    this.rpc = null;
    this.input = null;
    this.output = null;
    this.generation = 0;
    this.onstatechange = () => {
      if (this.rpc && (this.input.state !== 'connected' || this.output.state !== 'connected')) this.close();
    };
    access.addEventListener('statechange', this.onstatechange);
  }
  static async request(navigatorObject = navigator) {
    if (!navigatorObject.requestMIDIAccess) throw new Error('Web MIDI is unavailable in this browser.');
    return new MidiConnection(await navigatorObject.requestMIDIAccess({sysex: true}));
  }
  ports() {
    return {
      inputs: [...this.access.inputs.values()].filter(p => p.state === 'connected').map(p => ({id: p.id, name: p.name || p.id})),
      outputs: [...this.access.outputs.values()].filter(p => p.state === 'connected').map(p => ({id: p.id, name: p.name || p.id})),
    };
  }
  async open(inputId, outputId) {
    if (this.rpc || this.opening) throw new Error('Disconnect the current device first.');
    const input = this.access.inputs.get(inputId), output = this.access.outputs.get(outputId);
    if (!input || !output || input.state !== 'connected' || output.state !== 'connected') throw new Error('MIDI port unavailable.');
    const generation = this.generation; this.opening = true;
    try {
      await input.open(); await output.open();
      if (generation !== this.generation || input.state !== 'connected' || output.state !== 'connected') throw new Error('MIDI port disconnected.');
      this.input = input; this.output = output; this.rpc = new MidiRpc(input, output);
      const rx = await this.rpc.call(1);
      if (rx.length < 7 || 7 + rx[6] > rx.length) throw new Error('Invalid device identification.');
      const name = new TextDecoder('windows-1252').decode(rx.subarray(7 + rx[6])).split('\0')[0];
      if (!/syntakt/i.test(name)) throw new Error('Unsupported device.');
      return name;
    } catch (error) {
      this.close();
      // Either open may have succeeded before the error.
      await Promise.allSettled([input.close(), output.close()]);
      throw error;
    } finally { this.opening = false; }
  }
  close() {
    this.generation++;
    this.rpc?.close(); this.rpc = null;
    const input = this.input, output = this.output;
    this.input = this.output = null;
    if (input) void input.close().catch(() => {});
    if (output) void output.close().catch(() => {});
  }
  dispose() { this.close(); this.access.removeEventListener('statechange', this.onstatechange); }
}
