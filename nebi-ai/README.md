# `nebi-ai` — DeepAgents Pet Robot Agent & Web UI

> [!WARNING]
> **Unofficial project, not affiliated with the robot's manufacturer.** This software physically moves the robot;
> supervise it and keep a clear space around it. Provided as is, without warranty ([Apache-2.0](LICENSE),
> see [NOTICE](NOTICE)).

`nebi-ai` turns your Beni robot (LZ110/LZ100) into an autonomous AI pet powered by
[LangChain DeepAgents](https://github.com/langchain-ai/deepagents), with persistent long-term memory stored in your
home directory (`~/.nebi-ai/memories/`) and a built-in Web UI for chatting, tuning parameters, inspecting live robot
status, and editing memories.

## Features

- **DeepAgents harness**: Planning, tool use, and persistent filesystem memory via `create_deep_agent` +
  `FilesystemBackend(root_dir=~/.nebi-ai/memories, virtual_mode=True)`.
- **Multi-provider LLMs**: Defaults to Google Gemini (`google_genai:gemini-2.5-flash`), and supports OpenAI,
  Anthropic, Ollama, or any LangChain `provider:model` string.
- **Safe robot control**: Executes all robot actions through `nebi-cli`'s bounded `AgentRuntime` (obstacle stops,
  distance caps, carried-state checks, instant `STOP`).
- **Autonomous pet life**: Periodic sensory ticks let the pet observe, greet people, express emotions with eyes/voice/body,
  and update `/MEMORY.md` — respecting quiet hours and keeping autonomous locomotion opt-in by default.
- **Built-in Web UI**: Live chat with tool-call pills, camera snapshot, quick controls, emergency `STOP`, live
  parameter tuning, and a memory file editor.

## Quickstart

Requires Python 3.11+.

```bash
export GOOGLE_API_KEY="your-gemini-api-key"   # GEMINI_API_KEY also works

# Try with the built-in simulator + Web UI at http://127.0.0.1:8780
nebi-ai run --sim --open

# Or connect to a real robot on your LAN
nebi-ai run --host 192.168.1.50 --open

# Or attach to an already-running `nebi-cockpit serve` (:8000) so you share the robot link
nebi-ai run --daemon-url http://127.0.0.1:8000 --open
```

### Other LLM providers

Install the optional LangChain provider extra and pass `--model`:

```bash
pip install "nebi-ai[openai]"
export OPENAI_API_KEY="sk-..."
nebi-ai run --sim --model openai:gpt-4o

pip install "nebi-ai[anthropic]"
export ANTHROPIC_API_KEY="sk-ant-..."
nebi-ai run --sim --model anthropic:claude-sonnet-4-20250514
```

### Other CLI commands

```bash
# Send a single chat message from the terminal
nebi-ai chat --sim "Look around, say hi, and remember my name is Alex"

# Inspect or reset persistent memories in ~/.nebi-ai/memories/
nebi-ai memory list
nebi-ai memory show MEMORY.md
nebi-ai memory reset
```

## Persistent Memory Layout

By default, `nebi-ai` stores all state in `~/.nebi-ai/` (override with `--memory-dir` or `$NEBI_AI_HOME`):

```text
~/.nebi-ai/
├── config.json         # Saved model, temperature, personality, autonomy & safety settings
├── history.jsonl       # Chat and autonomous pet turn log
└── memories/           # DeepAgents virtual filesystem root
    ├── MEMORY.md       # Loaded automatically into the pet's context on every turn
    └── ...             # Additional notes created by the pet (e.g. people.md, rooms.md)
```
