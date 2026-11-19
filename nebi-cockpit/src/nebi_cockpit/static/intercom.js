// Nebi two-way intercom (browser side). Talks to /ws/intercom; see nebi_cockpit/intercom_ws.py.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const ui = () => window.NebiUI || { toast: (m) => console.log(m), isTyping: () => false };

  const ic = {
    ws: null,
    ctx: null,
    mic: null, // AudioWorkletNode
    speaker: null, // AudioWorkletNode
    stream: null, // MediaStream
    joined: false,
    talking: false,
    wantTalk: false, // PTT pressed (or open mic)
    mode: "ptt", // "ptt" | "open"
    micError: "",
    robotPeak: 0,
    micPeak: 0,
    rxFrames: 0,
    connected: false,
    starting: false,
    awaitingJoin: false, // sent "start", waiting for a state message that confirms it
  };

  const MODE_KEY = "nebi.intercom.mode";
  const VOL_KEY = "nebi.intercom.volume";
  try {
    ic.mode = localStorage.getItem(MODE_KEY) === "open" ? "open" : "ptt";
  } catch (_) {}

  // ---------------------------------------------------------------- UI

  function setPill(text, kind) {
    const pill = $("ic-pill");
    if (!pill) return;
    pill.textContent = text;
    pill.className = `ic-pill ${kind}`;
  }

  function render() {
    const btn = $("btn-ic-start");
    const talk = $("btn-ic-talk");
    if (!btn || !talk) return;
    btn.classList.toggle("btn-primary", !ic.joined);
    btn.classList.toggle("btn-danger-soft", ic.joined);
    btn.querySelector("span").textContent = ic.starting ? "Connecting…" : ic.joined ? "End intercom" : "Start intercom";
    btn.disabled = ic.starting;

    const micOk = !!ic.mic && !ic.micError;
    talk.disabled = !ic.joined || !micOk;
    talk.classList.toggle("talking", ic.talking);
    talk.querySelector("span").textContent =
      ic.mode === "open" ? (ic.talking ? "Mic open · click to mute" : "Mic muted · click to open") : ic.talking ? "Talking…" : "Hold to talk";

    for (const b of document.querySelectorAll("#ic-mode button")) b.classList.toggle("active", b.dataset.mode === ic.mode);

    const hint = $("ic-hint");
    if (ic.micError) hint.textContent = ic.micError;
    else if (!ic.joined) hint.textContent = "Hear Nebi's microphone and talk through its speaker. Headphones avoid echo in open-mic mode.";
    else if (ic.mode === "ptt") hint.textContent = "Hold the button or the T key to talk.";
    else hint.textContent = "Open mic: both directions are live. Press T to mute or unmute.";

    if (!ic.joined) setPill("Off", "off");
    else if (!ic.connected) setPill("Robot offline", "warn");
    else if (ic.talking) setPill("Talking", "talk");
    else setPill("Live", "live");
  }

  function meters() {
    const r = $("ic-meter-robot");
    const m = $("ic-meter-mic");
    if (r) r.style.setProperty("--lvl", Math.min(1, Math.sqrt(ic.robotPeak) * 1.1).toFixed(3));
    if (m) m.style.setProperty("--lvl", Math.min(1, Math.sqrt(ic.talking ? ic.micPeak : 0) * 1.1).toFixed(3));
    requestAnimationFrame(meters);
  }

  // ---------------------------------------------------------------- audio graph

  async function ensureAudio() {
    if (ic.ctx) {
      if (ic.ctx.state === "suspended") await ic.ctx.resume();
      return;
    }
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("This browser has no Web Audio support");
    ic.ctx = new Ctx({ latencyHint: "interactive" });
    if (!ic.ctx.audioWorklet) throw new Error("This browser lacks AudioWorklet (needs HTTPS or localhost)");
    await ic.ctx.audioWorklet.addModule("/static/intercom-worklet.js?v=1");

    ic.speaker = new AudioWorkletNode(ic.ctx, "nebi-speaker", { numberOfInputs: 0, outputChannelCount: [2] });
    ic.speaker.port.onmessage = (e) => {
      ic.robotPeak = e.data.peak || 0;
    };
    ic.speaker.connect(ic.ctx.destination);
    applyVolume();
    if (ic.ctx.state === "suspended") await ic.ctx.resume();
  }

  async function ensureMic() {
    if (ic.mic || ic.micError) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      ic.micError = "Microphone needs HTTPS or localhost — open the cockpit via http://127.0.0.1 or HTTPS. Listening still works.";
      return;
    }
    try {
      ic.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      ic.micError =
        err && err.name === "NotAllowedError"
          ? "Microphone permission denied — allow it in the browser to talk. Listening still works."
          : `Microphone unavailable (${(err && err.message) || err}). Listening still works.`;
      return;
    }
    const src = ic.ctx.createMediaStreamSource(ic.stream);
    ic.mic = new AudioWorkletNode(ic.ctx, "nebi-mic", { numberOfOutputs: 0, channelCount: 1, channelCountMode: "explicit" });
    ic.mic.port.onmessage = (e) => {
      ic.micPeak = e.data.peak || 0;
      if (e.data.pcm && ic.talking && ic.ws && ic.ws.readyState === WebSocket.OPEN && ic.ws.bufferedAmount < 64000) {
        ic.ws.send(e.data.pcm);
      }
    };
    src.connect(ic.mic);
  }

  function releaseMic() {
    if (ic.stream) for (const t of ic.stream.getTracks()) t.stop();
    if (ic.mic) ic.mic.disconnect();
    ic.stream = null;
    ic.mic = null;
    ic.micError = "";
  }

  function applyVolume() {
    const v = Number($("ic-volume")?.value ?? 100) / 100;
    if (ic.speaker) ic.speaker.port.postMessage({ gain: v * v * 2 }); // 0..200%, perceptual curve
  }

  // ---------------------------------------------------------------- socket

  function openSocket() {
    return new Promise((resolve, reject) => {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws/intercom`);
      ws.binaryType = "arraybuffer";
      let settled = false;
      ws.onopen = () => {
        settled = true;
        resolve(ws);
      };
      ws.onerror = () => {
        if (!settled) reject(new Error("Intercom socket failed"));
      };
      ws.onmessage = (e) => {
        if (typeof e.data !== "string") {
          ic.rxFrames++;
          if (ic.speaker) ic.speaker.port.postMessage({ pcm: e.data }, [e.data]);
          return;
        }
        let msg;
        try {
          msg = JSON.parse(e.data);
        } catch (_) {
          return;
        }
        if (msg.type === "state") {
          ic.connected = !!msg.connected;
          if (msg.joined) ic.awaitingJoin = false;
          else if (ic.joined && !ic.awaitingJoin) ic.joined = false; // server dropped us (e.g. robot switched)
          ic.talking = !!msg.talking && ic.wantTalk;
          render();
        } else if (msg.type === "error") {
          ui().toast(`Intercom: ${msg.message}`, "error");
          if (ic.awaitingJoin) {
            ic.awaitingJoin = false;
            ic.joined = false;
            render();
          }
          if (ic.wantTalk && /talk/i.test(msg.message)) setTalk(false);
        }
      };
      ws.onclose = () => {
        if (ic.ws !== ws) return;
        const was = ic.joined;
        ic.ws = null;
        ic.joined = false;
        ic.talking = false;
        ic.wantTalk = false;
        if (ic.mic) ic.mic.port.postMessage({ enabled: false });
        render();
        if (was) {
          ui().toast("Intercom disconnected — reconnecting…", "info");
          setTimeout(() => {
            if (!ic.joined && !ic.ws) start().catch(() => {});
          }, 1500);
        }
      };
    });
  }

  function send(obj) {
    if (ic.ws && ic.ws.readyState === WebSocket.OPEN) ic.ws.send(JSON.stringify(obj));
  }

  // ---------------------------------------------------------------- actions

  async function start() {
    if (ic.joined || ic.starting) return;
    ic.starting = true;
    render();
    try {
      await ensureAudio();
      await ensureMic(); // non-fatal: listen-only when the mic is unavailable
      if (!ic.ws) {
        try {
          ic.ws = await openSocket();
        } catch (err) {
          // A cockpit server started before the intercom existed serves the new UI from disk but has no route
          const probe = await fetch("/api/audio/intercom").catch(() => null);
          if (probe && probe.status === 404) {
            throw new Error("this cockpit server is older than the intercom — restart `nebi-cockpit serve` to enable it");
          }
          throw err;
        }
      }
      if (ic.speaker) ic.speaker.port.postMessage({ flush: true });
      ic.awaitingJoin = true;
      send({ type: "start" });
      ic.joined = true;
      if (ic.mode === "open" && ic.mic) setTalk(true);
    } catch (err) {
      ui().toast(`Intercom: ${(err && err.message) || err}`, "error");
    } finally {
      ic.starting = false;
      render();
    }
  }

  function stop() {
    setTalk(false);
    send({ type: "stop" });
    ic.joined = false;
    const ws = ic.ws;
    ic.ws = null;
    if (ws) setTimeout(() => ws.close(), 100);
    releaseMic();
    if (ic.speaker) ic.speaker.port.postMessage({ flush: true });
    render();
  }

  function setTalk(on) {
    on = !!on && ic.joined && !!ic.mic;
    if (on === ic.wantTalk) return;
    ic.wantTalk = on;
    ic.talking = on; // optimistic; server confirms in the next state message
    ic.mic?.port.postMessage({ enabled: on });
    send({ type: "talk", on });
    render();
  }

  function setMode(mode) {
    ic.mode = mode === "open" ? "open" : "ptt";
    try {
      localStorage.setItem(MODE_KEY, ic.mode);
    } catch (_) {}
    setTalk(ic.mode === "open" && ic.joined);
    render();
  }

  // ---------------------------------------------------------------- wiring

  function init() {
    const btn = $("btn-ic-start");
    if (!btn) return;
    btn.addEventListener("click", () => (ic.joined ? stop() : start()));

    const talk = $("btn-ic-talk");
    talk.addEventListener("pointerdown", (e) => {
      if (talk.disabled) return;
      if (ic.mode === "open") return void setTalk(!ic.wantTalk);
      talk.setPointerCapture(e.pointerId);
      setTalk(true);
    });
    const release = () => ic.mode === "ptt" && setTalk(false);
    talk.addEventListener("pointerup", release);
    talk.addEventListener("pointercancel", release);
    talk.addEventListener("contextmenu", (e) => e.preventDefault());

    for (const b of document.querySelectorAll("#ic-mode button")) b.addEventListener("click", () => setMode(b.dataset.mode));

    const vol = $("ic-volume");
    try {
      const saved = localStorage.getItem(VOL_KEY);
      if (saved !== null) vol.value = saved;
    } catch (_) {}
    vol.addEventListener("input", () => {
      applyVolume();
      try {
        localStorage.setItem(VOL_KEY, vol.value);
      } catch (_) {}
    });

    // T: hold to talk (push-to-talk) / toggle mute (open mic). Capture phase so drive keys don't see it.
    window.addEventListener(
      "keydown",
      (e) => {
        if (e.code !== "KeyT" || e.metaKey || e.ctrlKey || e.altKey || ui().isTyping()) return;
        if (!ic.joined || !ic.mic) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        if (e.repeat) return;
        setTalk(ic.mode === "open" ? !ic.wantTalk : true);
      },
      true
    );
    window.addEventListener(
      "keyup",
      (e) => {
        if (e.code === "KeyT" && ic.mode === "ptt" && ic.wantTalk) setTalk(false);
      },
      true
    );
    window.addEventListener("blur", () => ic.mode === "ptt" && setTalk(false));
    window.addEventListener("pagehide", () => ic.joined && send({ type: "stop" }));

    render();
    requestAnimationFrame(meters);
  }

  window.NebiIntercom = { start, stop, setTalk, state: ic };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
