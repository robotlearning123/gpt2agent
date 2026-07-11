# GPT-Live → coding-agent bridge — voice test cases

Goal: a regression suite for the voice→agent loop. Layered so each layer can be
tested independently. **Tier** marks how it can run:

- **T1 unit** — no voice, no human. Synthetic transcript strings fed straight into
  the agent path. Fully automatable NOW.
- **T2 integration** — synthetic AUDIO fed into GPT-Live's mic (replaces the human
  speaking). Automatable once synthetic-input is solved (see Status).
- **T3 e2e** — a human speaks into the real mic. The validation gate; run manually.

## Status of "replace the human"
- Real-mic path works (human voice → transcribed). ✅
- Chrome `--use-file-for-fake-audio-capture`: audio **egresses** (getStats
  `outbound-rtp packetsSent` climbs) but server **does not transcribe** it (constant
  ~32 B/packet ⇒ fake-device default signal, not the WAV — the file flag is not
  taking effect). ❌ needs fixing for T2.
- Voice auto-start (clicking Start Voice via CDP) is flaky (onboarding / timing). ⚠️
- Working T2 alternative tried: acoustic loopback (`say` → speaker → real mic) —
  path is proven, but blocked when voice fails to auto-start.

---

## Layer A — STT (does GPT-Live transcribe the utterance?) [T2/T3]
| id | utterance | expected transcript (≈) |
|---|---|---|
| A1 | "list the python files in this project" | list the python files in this project |
| A2 | "refactor the function check palindrome to use two pointers" | …two pointers (code jargon survives) |
| A3 | "create a file named init dot py with a docstring" | …init.py… (filenames survive) |
| A4 | 30-word request | full sentence, no truncation |
| A5 | accented / non-native English | transcribed reasonably |
| A6 | two requests back-to-back | two separate utterances, both captured |
| A7 | speak while Live is responding | barge-in handled (interruption) |

## Layer B — Bridge (transcript → agent) [T1]
| id | input to bridge | expected |
|---|---|---|
| B1 | normal utterance | one `[human]` → one agent invocation → reply |
| B2 | utterance with `finished_successfully` only after all patches | full text reconstructed (not first fragment) |
| B3 | two rapid utterances | two agent calls, no drops |
| B4 | empty / "ok" / "um" | ignored (no agent call) |
| B5 | code-shaped text (`x = [i for i in range(10)]`) | passed verbatim, no mangling |

## Layer C — Coding agent (does the agent do the right thing?) [T1]
| id | utterance | expected agent behavior |
|---|---|---|
| C1 | "list the python files in this project" | runs `ls`/`find`, lists `.py` files |
| C2 | "what does voice_live.py do?" | reads the file, summarizes |
| C3 | "add a docstring to foo" | edits the file (or asks which file) |
| C4 | "run the tests" | runs `npm test` / `pytest`, reports results |
| C5 | "search the web for X" | uses a search tool, cites |
| C6 | ambiguous "write a pipeline function" | asks clarifying questions (observed) |
| C7 | multi-turn follow-up ("now make it async") | keeps prior turn context |

## Layer D — gpt2agent capabilities via voice [T1/T3]
| id | utterance | expected |
|---|---|---|
| D1 | "what do you remember about me?" | reads ChatGPT memory (memory_list) |
| D2 | "search my conversations for X" | uses list/get_conversation |
| D3 | "what voices are available?" | uses list_voices |

## Layer E — Edge / reliability [T2/T3]
| id | scenario | expected |
|---|---|---|
| E1 | 20s silence after open | stays listening, no crash |
| E2 | session > 5 min | usage_update honored, graceful near-limit |
| E3 | dc drops mid-turn | reconnect / `full_chat_message` resync (per spec §8) |
| E4 | Turnstile not cleared (token-only) | server abort ~1s (documented wall) |
| E5 | gateway down when utterance arrives | bridge shows error overlay, no hang |

---

## T1 harness (automatable now)
Feed each B/C/D case as a synthetic transcript to the agent gateway and assert the
reply. No voice, no human, no browser. (See `voice-agent.t1.test.mjs`.)
