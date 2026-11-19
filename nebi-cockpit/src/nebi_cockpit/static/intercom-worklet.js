// AudioWorklet processors for the Nebi intercom.
// Wire format to/from the server: Int16 PCM, 48 kHz, mono, 960-sample (20 ms) frames.
// Both processors resample between the AudioContext rate and 48 kHz, so any device rate works.

const WIRE_RATE = 48000;
const FRAME = 960;

// Microphone -> 20 ms Int16 frames at 48 kHz (posted to the main thread as transferable buffers).
class NebiMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / WIRE_RATE; // input samples per output sample
    this.pos = 0; // fractional read position into `buf`
    // Preallocated work buffer: [carry (a sample or two from the previous quantum) | current quantum].
    // Reused every quantum so the audio thread never allocates (GC pauses = audible glitches).
    this.buf = new Float32Array(512);
    this.carryLen = 0;
    this.out = new Int16Array(FRAME);
    this.outLen = 0;
    this.peak = 0;
    this.enabled = false;
    this.port.onmessage = (e) => {
      if (e.data && typeof e.data.enabled === "boolean") {
        this.enabled = e.data.enabled;
        this.outLen = 0;
      }
    };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    let pk = 0;
    for (let i = 0; i < ch.length; i++) {
      const a = Math.abs(ch[i]);
      if (a > pk) pk = a;
    }
    this.peak = Math.max(pk, this.peak * 0.9);
    if (!this.enabled) {
      this.carryLen = 0;
      this.pos = 0;
      return true;
    }
    // Linear-interpolation resample of [carry + ch] to 48 kHz
    const total = this.carryLen + ch.length;
    if (this.buf.length < total) {
      const grown = new Float32Array(total * 2); // only if the render quantum ever grows
      grown.set(this.buf.subarray(0, this.carryLen));
      this.buf = grown;
    }
    const buf = this.buf;
    buf.set(ch, this.carryLen);
    let p = this.pos;
    while (p + 1 < total) {
      const i = p | 0;
      const f = p - i;
      const s = buf[i] + (buf[i + 1] - buf[i]) * f;
      this.out[this.outLen++] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
      if (this.outLen === FRAME) {
        const frame = this.out.slice(0);
        this.port.postMessage({ pcm: frame.buffer, peak: this.peak }, [frame.buffer]);
        this.outLen = 0;
      }
      p += this.step;
    }
    const keep = Math.min(Math.floor(p), total);
    buf.copyWithin(0, keep, total);
    this.carryLen = total - keep;
    this.pos = p - keep;
    return true;
  }
}

// 48 kHz Int16 frames from the robot -> speaker, with a small adaptive jitter buffer.
class NebiSpeakerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = WIRE_RATE / sampleRate; // wire samples consumed per output sample
    this.queue = []; // Float32Array chunks at 48 kHz
    this.cur = null;
    this.curPos = 0;
    this.buffered = 0; // samples queued (48 kHz)
    this.primed = false;
    this.prime = FRAME * 3; // start after 60 ms buffered
    this.maxBuffered = FRAME * 12; // >240 ms: drop old audio to keep latency low
    this.gain = 1;
    this.peak = 0;
    this.a = 0;
    this.b = 0;
    this.frac = 0;
    this.underruns = 0;
    this.tick = 0;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.pcm) {
        const i16 = new Int16Array(d.pcm);
        const f = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f[i] = i16[i] / 32768;
        this.queue.push(f);
        this.buffered += f.length;
        while (this.buffered > this.maxBuffered && this.queue.length > 1) {
          this.buffered -= this.queue.shift().length;
        }
      } else if (typeof d.gain === "number") {
        this.gain = d.gain;
      } else if (d.flush) {
        this.queue = [];
        this.cur = null;
        this.curPos = 0;
        this.buffered = 0;
        this.primed = false;
        this.a = 0;
        this.b = 0;
        this.frac = 0;
      }
    };
  }

  nextSample() {
    if (!this.cur || this.curPos >= this.cur.length) {
      this.cur = this.queue.shift() || null;
      this.curPos = 0;
      if (!this.cur) return null;
    }
    this.buffered--;
    return this.cur[this.curPos++];
  }

  process(_inputs, outputs) {
    const out = outputs[0];
    const ch0 = out[0];
    if (!this.primed && this.buffered >= this.prime) this.primed = true;
    let pk = 0;
    for (let i = 0; i < ch0.length; i++) {
      let s = 0;
      if (this.primed) {
        // Linear resample 48 kHz -> context rate between wire samples a (prev) and b (next)
        s = (this.a + (this.b - this.a) * this.frac) * this.gain;
        this.frac += this.step;
        while (this.frac >= 1) {
          this.frac -= 1;
          const n = this.nextSample();
          if (n === null) {
            this.primed = false; // underrun: go silent and re-prime
            this.underruns++;
            this.a = this.b = 0;
            this.frac = 0;
            break;
          }
          this.a = this.b;
          this.b = n;
        }
      }
      ch0[i] = s;
      const a = Math.abs(s);
      if (a > pk) pk = a;
    }
    for (let c = 1; c < out.length; c++) out[c].set(ch0);
    this.peak = Math.max(pk, this.peak * 0.9);
    if (++this.tick % 8 === 0) {
      this.port.postMessage({ peak: this.peak, bufferedMs: Math.round((this.buffered / WIRE_RATE) * 1000), underruns: this.underruns });
    }
    return true;
  }
}

registerProcessor("nebi-mic", NebiMicProcessor);
registerProcessor("nebi-speaker", NebiSpeakerProcessor);
