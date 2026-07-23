# Path C — AI Persona Avatar ("AI VTuber") (scope)

Give the avatar a brain and a voice: a character you can talk to (or that talks on its
own), where the **mouth is driven by the speech audio** instead of your face. Same
avatar, same OBS pipeline — you just flip it from "puppet me" to "autonomous AI."

Status: **planning**. This builds on Path B (the avatar renders and can already move its
mouth via visemes). Nothing here is built yet.

---

## The loop

```
you speak / type ─▶ Claude (persona brain, streaming) ─▶ TTS (voice)
                                                            │
                              audio plays ◀─────────────────┘
                                    │
                          Web Audio analyser ─▶ mouth visemes  (avatar "talks")
```

The key idea vs. Path B: in puppet mode your **face** drives the mouth; in AI mode the
**audio** drives it. Everything else (three-vrm render, transparent canvas → OBS) is
unchanged. An AI-VTuber scene is just `/avatar?obs=1&ai=1`.

## Components

| Piece | Role | MVP choice | Upgrade |
|---|---|---|---|
| **Brain** | persona + responses | **Claude** via the Anthropic API, streaming (Sonnet 5 or Haiku 4.5 for low latency; Opus 4.8 for depth) | tools, memory, RAG |
| **Voice (TTS)** | text → speech | browser `SpeechSynthesis` (free, offline, robotic) | ElevenLabs / Cartesia / a good TTS API (natural, pick a voice) |
| **Lip-sync** | audio → mouth | Web Audio `AnalyserNode` → amplitude → jaw/`aa`/`oh` | phoneme→viseme mapping for accurate mouth shapes |
| **Input** | how you talk to it | text box | mic **STT** (browser `SpeechRecognition` or Whisper) for real voice chat |
| **Persona** | character definition | a system prompt / "character card" (name, personality, speaking style) | per-character voices, mood, memory |

## Architecture in video-stream

- **Keys live on the server, never the browser.** Add a small proxy so the avatar page
  never sees API keys:
  - `POST /api/persona/chat` — streams Claude's reply (SSE/WebSocket). Reads
    `ANTHROPIC_API_KEY` from the environment.
  - `POST /api/persona/tts` — returns audio for a chunk of text (only if using a paid TTS;
    browser `SpeechSynthesis` needs no server).
  - Persona/system prompt from a config file or a small UI.
- **Client (avatar page), AI mode:**
  1. Input (text or mic STT) → `POST /api/persona/chat`.
  2. Stream tokens; as each **sentence** completes, send it to TTS so speech starts fast.
  3. Play audio through Web Audio; an `AnalyserNode` drives the mouth visemes each frame
     (reuse the existing `expr("aa"/"oh"/…)` rig from Path B).
  4. Face tracking is off in AI mode; audio owns the mouth. (Optional: still use your head
     tracking for head motion while the AI drives the mouth — a fun hybrid.)

## Modes (how it fits the existing avatar)

- **Puppet** (Path B, today): your face drives everything.
- **AI** (Path C): autonomous character — you talk/type, it thinks, speaks, lip-syncs.
- **Hybrid** (later): AI voice drives the mouth while *your* head/pose still drives motion —
  or a "take over" toggle to grab the avatar live (ties into your multi-machine OBS flow).

## Phases

- **C1 — prove the loop:** text box → Claude (streaming, server proxy) → browser
  `SpeechSynthesis` → amplitude lip-sync. All free except the Claude API. Confirms
  brain→voice→mouth end to end.
- **C2 — voice in:** mic STT (browser `SpeechRecognition`) → real spoken conversation.
- **C3 — good voice:** swap in a quality TTS (ElevenLabs/Cartesia) with a chosen voice;
  sentence-chunked streaming to cut latency; better visemes.
- **C4 — personality:** richer character card, short-term memory, idle chatter, reactions.
- **C5 — hybrid/takeover:** blend with puppet mode; hand off between "AI drives" and "I
  drive," integrated with the OBS source URLs.

## Honest caveats

- **Latency is the hard part.** input → LLM → TTS → audio has to feel snappy. Mitigate
  with streaming + speaking sentence-by-sentence (don't wait for the full reply), and a
  fast model tier. Real-time voice-to-voice is genuinely tricky to make feel natural.
- **Cost.** LLM + paid TTS are per-use. Browser TTS is free but sounds robotic. Start free
  (C1) to validate before paying for voice quality.
- **Lip-sync fidelity.** Amplitude-driven jaw is convincing enough for MVP; true
  phoneme-accurate visemes are a bigger lift.
- **Secrets.** API keys stay in server env only — never shipped to the browser or committed.
- **Barge-in / interruptions** (talking over it) is an advanced feature; skip for MVP.

## Decisions needed before building

1. **Voice quality:** start with free browser TTS (robotic, instant, offline), or go
   straight to a paid TTS for a natural voice? (C1 recommends free first.)
2. **Input:** text-only to start, or mic voice-chat from the outset?
3. **Persona:** one character, or switchable character cards? What's her personality/voice?
4. **Autonomy:** purely reactive (responds when you talk), or does she also idle-chatter /
   react to things on her own?

---

See [`path_b.md`](path_b.md) for the avatar foundation this builds on.
