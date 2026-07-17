---
description: "The Audio Understanding (audio_understanding) built-in tool and its Tool Variable MAX_AUDIO_BYTES (max audio size sent to the audio model). How to view and change the audio_understanding limit per profile, and which models accept audio input."
---

# Audio Understanding Tool (audio_understanding)

The **Audio Understanding** tool (`tool_id` `audio_understanding`) sends an audio
clip to an audio-capable model and answers questions about it — transcribe
speech, summarize a recording, or identify sounds. It mirrors Image
Understanding: it runs through the dedicated **Specialized Audio Model** (Settings
→ LLM Providers), falling back to the main model when unset, and is exposed only
when the model that would run it accepts audio input. Audio is sent as an
`input_audio` content part; supported formats are WAV, MP3, M4A, AAC, OGG, FLAC,
and WebM (no transcoding — oversized or unsupported files are rejected).

Not every model accepts audio. Audio-capable models include OpenAI `gpt-audio`,
Google Gemini (2.0/2.5/3.x), xAI `grok-4-1-fast`, Mistral `voxtral-small-latest`,
Qwen `qwen3-omni-flash`, and NVIDIA Nemotron Omni. Anthropic (Claude) models have
no audio input. Choosing a model that can't hear surfaces a clear
`AudioNotSupported` error rather than a silent failure.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `MAX_AUDIO_BYTES` | number | `26214400` (25 MB) | Maximum audio size in bytes sent to the audio model. Larger files are rejected (audio is not transcoded or trimmed). |

`audio_understanding` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Audio Understanding.
- **CLI** — `cremind tools set-var audio_understanding MAX_AUDIO_BYTES=52428800`;
  `cremind tools get audio_understanding --json` to read current values.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
