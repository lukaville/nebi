# nebi-voice

> [!WARNING]
> **Independent, unofficial project.** nebi is not affiliated with, endorsed by or supported by the manufacturer of
> the Beni robot. "Beni" and the model names LZ110 / LZ100 are used only to identify the compatible hardware.
>
> - Use it only with a robot **you own**. Using it may **void your warranty** or violate the manufacturer's terms.
> - Through nebi / nebi-cli the voice drives a robot that **physically moves** (speech comes with gimbal nods and
>   expressions). Supervise the robot and keep a clear space around it.
> - Provided **AS IS, without warranty** of any kind (Apache-2.0, see the LICENSE and NOTICE files).

Small-robot text-to-speech for robots compatible with Beni, built on the 4.5M-parameter
[tiny-voca](https://huggingface.co/n4ze3m/tiny-voca) ONNX model. CPU-only, about 50–100× real time. Part of the
nebi monorepo; usable standalone (no nebi needed) or as a nebi TTS extension.

```bash
# inside the monorepo (shared venv nebi/.venv; scripts/dev-setup.sh does this):
.venv/bin/pip install -e "nebi-voice[dev]"
.venv/bin/nebi-voice say "Hello? Ready!"              # plays it
.venv/bin/nebi-voice say -p nebi "Hello, I am Nebi." -o hello.wav
.venv/bin/nebi-voice demo --out-dir samples             # demo lines x presets
.venv/bin/nebi-voice presets                            # all tunable parameters
.venv/bin/nebi-voice serve --open                       # web playground at http://127.0.0.1:8770
.venv/bin/nebi-voice download-model                     # fetch the model now (e.g. before going offline)
```

## The voice model (automatic download)

The tiny-voca model (~27 MB: ONNX graph + phonemizer data) is **not** shipped in the package or the repository; it
is downloaded from the Hugging Face Hub the first time a voice is rendered, into
`~/.cache/nebi-voice/tiny-voca` (`$XDG_CACHE_HOME/nebi-voice/…`; `%LOCALAPPDATA%\nebi-voice\…` on Windows).
`NEBI_VOICE_MODEL_DIR` overrides the location. An editable monorepo checkout also uses `nebi-voice/models/tiny-voca`
when present (gitignored; the tests use it), and an old `~/.cache/beni-voice/tiny-voca` is reused.

pip cannot run post-install hooks for wheels, so "on install" really means **on first use**: the first render, or
— as a nebi extension — the moment the cockpit or the `nebi` agent starts (the download runs in a background
thread; a Speak during the few seconds it takes fails with "voice model downloading (~27 MB), retry in a few
seconds"). Run `nebi-voice download-model` to prepare a machine explicitly. Downloads land in a `.partial`
directory first, so an interrupted download is never used. Offline without the model, rendering fails with
`ModelUnavailableError` (a `TtsError` in nebi) whose hint says what to do.

## As a nebi extension

Installed into the nebi venv, the entry point `nebi.extensions: nebi-voice = "nebi_voice.extension:extension"`
registers the TTS engine `nebi-voice` (priority 20, so it is the default over the OS `system` engine and the
built-in `chirp`) in both hosts: `nebi say 'Ball? 👀'` (nebi-cli) and the cockpit Speak panel. Its presets are the
ones below with `robot` (the robot-pet voice) first, i.e. the default; `--preset nebi` gives clear full sentences.
Babble (`nebi say --babble -e curious`) is mood-matched pet gibberish. `PYNEBI_TTS_ENGINE=chirp` or
`PYNEBI_DISABLE_EXTENSIONS=nebi-voice` turns it off. `import nebi_voice` never needs nebi (the `nebi` extra
exists only to install it next to a bare nebi).

Pet voice features:
- **Emotions:** `neutral happy excited curious sad scared sleepy grumpy love uh-oh`. Each bends pitch, melody, tempo,
  warble and loudness, and picks the pitch contour drawn on words.
- **Babble:** no text (or `--babble`) produces mood-matched gibberish ("Bee doo wee!"). Pets mostly vocalize.
- **Pitch glides:** the WORLD vocoder draws contours inside words (rise, question, droop, step-down "uh-oh",
  wobble) and stretches vowels ("Heee-looo?"), after flattening the TTS model's own melody.
- **Emoji set the mood:** `"Ball? 👀"` is spoken as curious, and the emoji is not pronounced.
- **Mastering for a small speaker:** compressor, gated loudness normalization, soft limiter, a 15 ms fade-in
  so it never startles, and `volume`.
- **Body-sync timeline:** `Utterance.timeline()` gives a 25 Hz loudness envelope, syllable beats and word
  timings, used to pulse the LED eyes and nod the gimbal.
- **Cache:** identical requests (same text, emotion, seed and parameters) return instantly.

```bash
nebi-voice say "Ball? 👀"                          # curious
nebi-voice say --babble -e excited
nebi-voice say "Uh-oh" -e uh-oh --timeline t.json -o uhoh.wav
```

```python
from nebi_voice import RobotVoice, to_pcm16
u = RobotVoice("robot").render("Hello?", emotion="curious")   # Utterance: audio + timeline()
v = RobotVoice("robot", pitch=7, formant=1.35)   # any VoiceParams field can be overridden
audio = v.synthesize("Where is my ball?")       # float32 mono, 24 kHz
v.save("Ta-da!", "tada.wav")
pcm48k = to_pcm16(audio, v.sample_rate)          # 48 kHz s16le mono (the robot intercom's format)
```

## How the voice works

A small-robot voice is a human voice resynthesized with a higher pitch and a smaller "throat", performed with an
exaggerated, questioning melody, and played through a small boxy speaker. Delivery is mostly short words
("Hel-looo?", "Ready!").

| Trait | nebi-voice technique |
|---|---|
| Resynthesized human voice | tiny-voca is itself a source-filter resynthesizer: a pulse train plus noise shaped by predicted filters. Its pitch, pitch range and speed are controlled at the source, so there are no pitch-shifter artifacts. |
| Raised pitch, small "throat" | Render at `pitch − 12·log2(k)` semitones and `speed/k`, then resample by `k`. Formants rise by `k` while pitch and duration land exactly on target (`formant`, `pitch`). |
| Performed, exaggerated inflection | `pitch_range` (~1.9×). Each sentence is rendered **once, fluently**, then cut at the model's word boundaries (`<sp>` tokens) and given pauses. `halting` (0..1) sets the pause amount and fades smoothly with sentence length (and carries over between sentences), so there is no hard switch between word-by-word and fluent speech. Function words lean on the next word ("Where… is my… ball?"). One WORLD pass adds per-word pitch bounce, and the last word's melody is replaced by the emotion contour. |
| Electronic warble / servo jitter | Vibrato (a sine plus a slow random drift), plus light tremolo. |
| Metallic, boxy body | A comb-filter "box" resonance, a little ring modulation and a light bitcrush. |
| Tiny loudspeaker | Band-limited to 280 Hz–6.8 kHz, with a presence bump and soft saturation. |
| Mechanical character | Optional servo whirs and clicks between phrases, and an electronic boop. |

Presets: `robot` (default; high, squeezed, halting, boxy), `probe` (sleek, brighter, ring-tone), `nebi` (friendly,
clear full sentences), `robot-low` / `robot-male` / `deep-robot` (male-character variants), `plain` (raw tiny-voca, for A/B).

The playground (`nebi-voice serve`) has sample texts, preset chips, a slider for every parameter (changed values are
highlighted; double-click a slider to reset it), a seed dice, a take history with downloads, and copy-as-CLI/Python.

Notes
- `seed` fixes every choice nebi-voice makes (per-word pitch, gaps, servo sounds). tiny-voca's breath noise is
  randomized inside the ONNX graph, so renders are not bit-identical.
- English only (the tiny-voca phonemizer). The model is limited to ~32 s per utterance; text is split into sentences.
- Licences: nebi-voice is Apache-2.0 (see LICENSE / NOTICE). The downloaded tiny-voca model and the bundled dizione
  phonemizer (`src/nebi_voice/_vendor/`) are Apache-2.0 (Muhammed Nazeem); the model's phonemizer data is derived from the CMU Pronouncing Dictionary
  (BSD-style) and Apache-2.0 homograph data. Its training audio is synthetic (no real speaker).

Sources: [tiny-voca model card](https://huggingface.co/n4ze3m/tiny-voca)
