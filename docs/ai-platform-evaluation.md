# AI Inference Platform Evaluation for Forgememo Managed Service

**Date:** 2026-04-01
**Context:** Selecting the optimal AI inference provider for Forgememo's managed
service, where users pay per-distillation and Forgememo keeps a margin.

## Business Model

```
User picks "forgememo" provider in terminal
  -> forgememo auth login (magic link)
  -> session-end distillation triggers managed inference
  -> User pays: (token cost + PLATFORM_FEE) from their credit balance
  -> Forgememo margin = PLATFORM_FEE + (charge_to_user - wholesale_cost)
```

The goal: **maximize margin per distillation** while keeping inference quality
high enough for structured JSON extraction (mining, distilling, session summaries).

## Workload Profile

| Metric | Value |
|--------|-------|
| Task | Extract structured JSON from raw tool events |
| Input tokens | ~500-2000 per event |
| Output tokens | ~300-500 (structured JSON) |
| Latency tolerance | High (background, session-end) |
| Quality bar | Reliable JSON output with correct field extraction |
| Scale | Per-user, per-session (bursty, not streaming) |

## Provider Comparison

### Tier 1: Direct API (recommended for margin)

| Provider | Model | Input $/M | Output $/M | Cost per distill* | Quality | Margin at $0.005 charge |
|----------|-------|-----------|------------|-------------------|---------|------------------------|
| **Google Gemini** | gemini-2.0-flash | $0.10 | $0.40 | ~$0.00035 | Good | **93%** |
| OpenAI | gpt-4o-mini | $0.15 | $0.60 | ~$0.00053 | Good | 89% |
| Anthropic | claude-haiku-4-5 | $0.80 | $4.00 | ~$0.0036 | Excellent | 28% |

*Assuming 1500 input tokens + 400 output tokens per distillation.

### Tier 2: Aggregators (NOT recommended - double margin)

| Provider | Model access | Their markup | Problem |
|----------|-------------|-------------|---------|
| OpenRouter | 100+ models | 5-20% on top | Middleman eats our margin |
| AWS Bedrock | Multi-provider | ~20% markup | Enterprise overhead |

### Tier 3: Self-hosted (future consideration at scale)

| Provider | Model | Fixed cost | Break-even |
|----------|-------|-----------|------------|
| GPU server (Hetzner/OCI) | Llama 3.1 8B | ~$150/mo | ~400K distillations/mo |
| Oracle Cloud free tier | Llama 3.1 8B | $0 (free A1) | Immediately |

## Decision: Google Gemini 2.0 Flash (Direct API)

### Why

1. **Lowest cost per distillation** among production-grade APIs (~$0.00035)
2. **93% margin** at a $0.005 per-distillation charge to users
3. **Reliable structured JSON output** - Flash is optimized for fast, structured tasks
4. **Already integrated** in forgememo's inference.py (BYOK path)
5. **No middleman** - direct API key, full margin control
6. **Generous free tier** for development/testing

### Why NOT Oracle AI

- Not a model aggregator - limited to Llama/Cohere models
- Complex OCI setup (IAM, networking, compartments) for a simple API call
- No Anthropic/OpenAI/Gemini models available
- Enterprise-oriented pricing and tooling overkill for our workload

### Why NOT OpenRouter

- Adds 5-20% margin on top of provider costs
- We ARE the margin layer - don't want another middleman
- Reduces our profit per distillation unnecessarily
- Good for BYOK users who want model flexibility, but not for our managed backend

### Fallback Strategy

1. **Primary:** Google Gemini 2.0 Flash (cheapest, good quality)
2. **Fallback:** OpenAI gpt-4o-mini (if Gemini is down)
3. **Premium tier (future):** Claude Haiku for users willing to pay more

## Competitor Analysis: Claude-mem

Claude-mem (Claude Code memory plugin) does NOT offer managed services.
Likely revenue model: open-source + consulting/enterprise, or no revenue (side project).

Forgememo's managed inference model is a **competitive advantage**:
- Recurring usage-based revenue
- Zero-friction onboarding (no API key needed)
- Stickier than BYOK (users don't want to manage keys)

## Implementation

Server changes required:
1. Replace `anthropic` SDK with `google-genai` in `server/main.py`
2. Update `_estimate_cost()` with Gemini Flash pricing
3. Update default model from `claude-haiku-4-5-20251001` to `gemini-2.0-flash`
4. Update `server/requirements.txt` (swap anthropic for google-genai)
5. Update `server/.env.example` (GEMINI_API_KEY replaces ANTHROPIC_API_KEY)
