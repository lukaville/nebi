import { $, on, post, setSegment, toast } from "./core.js";

// Sound tab: sound effects, audio file playback, voice wake-up, wireless mics (eye LEDs: eyes.js).

// ---------------------------------------------------------------- audio

on("sfx-list", "click", (e) => {
  const b = e.target.closest("[data-sfx]");
  if (b) post("/api/audio/sound_effect", { value: Number(b.dataset.sfx) }, `Played ${b.dataset.name}`);
});
on("btn-audio-play", "click", () => {
  const p = $("audio-path").value.trim();
  if (!p) return toast("Enter an audio file path on the robot", "info");
  post("/api/audio/play_file", { audio_path: p, play_action: 1 }, `Playing ${p}`);
});
on("btn-audio-stop", "click", () =>
  post("/api/audio/play_file", { audio_path: $("audio-path").value.trim(), play_action: 0 }, "Audio stopped")
);
on("chk-voice-wakeup", "change", (e) => post("/api/audio/voice_wakeup", { enabled: e.target.checked }, `Voice wake-up ${e.target.checked ? "on" : "off"}`));
on("btn-mic-scan", "click", () => post("/api/audio/mic/scan", { enabled: true }, "Scanning for wireless mics…"));
on("btn-mic-builtin", "click", async () => {
  if (await post("/api/audio/mic/select", { kind: 0, mac: "" }, "Using built-in mic array"))
    setSegment($("mic-source"), (b) => b.id === "btn-mic-builtin");
});
on("btn-mic-ble", "click", async () => {
  const mac = $("mic-mac").value.trim() || "AA:BB:CC:11:22:33";
  if (await post("/api/audio/mic/select", { kind: 1, mac }, `Using lav mic ${mac}`))
    setSegment($("mic-source"), (b) => b.id === "btn-mic-ble");
});
