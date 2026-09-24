# Fresh MCP deep_research_heavy result

- Tool error: `False`
- Elapsed: `144.6s`
- Tool: `deep_research_heavy`

## Final tool result

# Qwen3-4B vs. Gemma-4-12B-it Context Length

## Executive summary

**Gemma-4-12B-it has the substantially larger standard context:** 256K/262,144 tokens versus Qwen3-4B’s native 32,768 tokens. Qwen3-4B can reach 131,072 tokens with YaRN, but that is an explicitly scaled, validated extension rather than its native limit. Thus, Gemma provides **8× Qwen’s native context and 2× its YaRN-extended context**.

## Evidence and primary links

- **Qwen3-4B — official model card:** exact wording: “Context Length: 32,768 natively and 131,072 tokens with YaRN.” The card further says YaRN/RoPE scaling is recommended beyond the native limit and validated through 131,072 tokens. No inference is required. citeturn1view0
- **Gemma-4-12B-it — official model card/repository:** the model table explicitly states “Context Length | … 256K tokens” for 12B Unified; its official `config.json` sets `max_position_embeddings` to **262,144**. Thus, “256K” corresponds explicitly to 262,144 in the repository; no inference is required. citeturn1view1

## Implications

For document-scale retrieval, long conversations, coding repositories, and multimodal sequences, Gemma-4-12B-it offers materially more headroom without Qwen’s YaRN extension setup. Qwen3-4B remains capable of 128K-class workloads, but contexts above 32,768 depend on RoPE scaling rather than native operation.

## Assumptions

“Context length” means the model’s total supported sequence window, not maximum generated-output length. “256K” is interpreted according to Gemma’s accompanying configuration: **262,144 tokens**.

---
**Sources:**
- [Qwen/Qwen3-4B · Hugging Face](https://huggingface.co/Qwen/Qwen3-4B)
- [google/gemma-4-12B-it · Hugging Face](https://huggingface.co/google/gemma-4-12B-it)
