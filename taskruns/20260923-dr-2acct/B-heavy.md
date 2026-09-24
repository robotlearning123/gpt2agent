# Fresh MCP deep_research_heavy result

- Tool error: `False`
- Elapsed: `152.8s`
- Tool: `deep_research_heavy`

## Final tool result

# Context-Length Comparison: Qwen3-4B vs. Gemma-4-12B-it

## Executive summary

Google’s **Gemma-4-12B-it has the larger documented context window: 256K tokens**, versus **32,768 tokens natively for Qwen3-4B**, although Qwen reports validated extension to **131,072 tokens using YaRN**. Thus Gemma’s stated window is about **2× Qwen’s validated extended window and 8× its native window**. citeturn3view0turn2view1

## Qwen3-4B

Primary model card: [Qwen/Qwen3-4B](https://huggingface.co/Qwen/Qwen3-4B).

Under **“Model Overview,” line 218**, Qwen states exactly: **“Context Length: 32,768 natively and 131,072 tokens with YaRN.”** citeturn3view0

The **“Processing Long Texts”** section clarifies that **32,768** is native and that performance was validated “up to 131,072 tokens” with YaRN. Lines 469–470 additionally warn that YaRN may hurt shorter-text performance; the config’s `max_position_embeddings=40,960` should therefore not be mistaken for the advertised native context length. citeturn3view3

## Gemma-4-12B-it

Primary model card: [google/gemma-4-12B-it](https://huggingface.co/google/gemma-4-12B-it).

In **“Models Overview → Dense Models,” lines 132–140**, the **12B Unified** column gives exactly **“Context Length | 256K tokens.”** The **“Core Capabilities”** section independently says **“256K tokens (12B, 26B A4B/31B).”** citeturn2view1

## Conclusion

**Gemma-4-12B-it has the larger official context window: 256K tokens versus Qwen3-4B’s 32,768 native / 131,072 YaRN-extended tokens.** citeturn2view1turn3view1

---
**Sources:**
- [Qwen/Qwen3-4B · Hugging Face](https://huggingface.co/Qwen/Qwen3-4B)
- [google/gemma-4-12B-it · Hugging Face](https://huggingface.co/google/gemma-4-12B-it)
