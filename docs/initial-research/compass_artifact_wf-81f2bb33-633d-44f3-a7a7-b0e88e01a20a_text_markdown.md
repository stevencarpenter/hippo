# MLX on Apple Silicon: the definitive model and tooling guide

**MLX has matured into a production-viable inference and fine-tuning framework for Apple Silicon, with 9,532 models on
HuggingFace and measurable speed advantages over GGUF/llama.cpp in most workloads.** For a knowledge capture daemon
running on M3 Max 64GB (floor) through M5 Max 128GB (ceiling), MLX offers zero-copy unified memory access, **21–87%
higher throughput** than llama.cpp across tested models, and a complete ecosystem spanning inference, fine-tuning,
embeddings, and reranking — all native to Apple Silicon. The ecosystem's primary limitations are the absence of native
flash attention (impacting long-context prefill), coarser quantization granularity than GGUF, and a pool allocator that
retains memory rather than releasing it to the OS. This report covers every layer of the stack: available models,
tooling, fine-tuning, benchmarks, embeddings, and practical daemon configuration.

---

## Every major model family now ships in MLX format

The **mlx-community** organization on HuggingFace is the central hub, hosting thousands of conversions across 100+
curated collections. Two secondary publishers matter: **lmstudio-community** (LM Studio's team, often first to convert
bleeding-edge models) and **Qwen** (official MLX conversions). Here's what's available right now for the models you're
tracking:

**Qwen3.5** landed in MLX within days of release (March 2026). The flagship `mlx-community/Qwen3.5-397B-A17B-4bit` is a
MoE model at **~62GB on disk** (too large for 64GB, fits on 128GB). The more practical
`mlx-community/Qwen3.5-35B-A3B-4bit` is a vision+language MoE at roughly 5–6GB active memory. A dense
`mlx-community/Qwen3.5-9B-OptiQ-4bit` exists and fits comfortably on both machines.

**Qwen3-Coder-Next** has MLX conversions from lmstudio-community in both 4-bit and 8-bit (
`lmstudio-community/Qwen3-Coder-Next-MLX-4bit`). **Qwen2.5-Coder-14B-Instruct** is available at 4-bit (~8–9GB) and
8-bit (~15GB) from both mlx-community and lmstudio-community. **Qwen3-30B-A3B** (the MoE coding workhorse) exists as
`Qwen/Qwen3-30B-A3B-MLX-4bit` (official) and `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` with **3,060 downloads
** — one of the most popular coding models in the MLX ecosystem. At 4-bit, it requires ~17GB memory but activates only
3B parameters per token, delivering generation speeds comparable to a 3B dense model.

**GLM-4.7** is available but massive: the full 185B-parameter model in 4-bit still requires ~200GB on disk. The
practical option is `lmstudio-community/GLM-4.7-Flash-MLX-8bit`, the Flash variant (30B-A3B MoE architecture). *
*Devstral** ships as `mlx-community/Devstral-Small-2505-8bit` at 7B parameters. **Phi-4-Reasoning-Plus** is available
via `lmstudio-community/Phi-4-reasoning-plus-MLX-4bit`. **Gemma 3** has extensive coverage including the popular QAT (
Quantization-Aware Trained) variant `mlx-community/gemma-3-27b-it-qat-bf16` with **113K downloads**. **GPT-OSS** (
OpenAI's first open-weight model) ships in both 20B and 120B sizes with MXFP4 quantization.

| Model                      | MLX Repo                                           | Quant    | Disk Size | Memory  | Fits 64GB?   |
|----------------------------|----------------------------------------------------|----------|-----------|---------|--------------|
| Qwen3.5-9B                 | `mlx-community/Qwen3.5-9B-OptiQ-4bit`              | 4-bit    | ~5GB      | ~6GB    | ✅            |
| Qwen3-Coder-Next           | `lmstudio-community/Qwen3-Coder-Next-MLX-4bit`     | 4-bit    | ~varies   | ~varies | ✅            |
| Qwen2.5-Coder-14B-Instruct | `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit`    | 4-bit    | ~8–9GB    | ~9GB    | ✅            |
| Qwen3-Coder-30B-A3B        | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit`  | 4-bit    | ~17GB     | ~17GB   | ✅            |
| Phi-4-Reasoning-Plus       | `lmstudio-community/Phi-4-reasoning-plus-MLX-4bit` | 4-bit    | ~8GB      | ~9GB    | ✅            |
| Gemma 3 27B QAT            | `mlx-community/gemma-3-27b-it-qat-bf16`            | QAT bf16 | ~14GB     | ~15GB   | ✅            |
| GPT-OSS 20B                | `mlx-community/gpt-oss-20b-MXFP4-Q4`               | MXFP4+Q4 | ~12GB     | ~12GB   | ✅            |
| DeepSeek-R1-Distill-32B    | `mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit`  | 4-bit    | ~17GB     | ~18GB   | ✅            |
| Llama 3.3 70B              | `mlx-community/Llama-3.3-70B-Instruct-4bit`        | 4-bit    | ~38GB     | ~40GB   | ⚠️ tight     |
| Qwen3.5-397B-A17B          | `mlx-community/Qwen3.5-397B-A17B-4bit`             | 4-bit    | ~62GB     | ~65GB   | ❌ 128GB only |

---

## MLX quantization uses a different paradigm than GGUF

MLX quantization operates on a fundamentally different system than GGUF's Q4_K_M/Q5_K_M nomenclature. The
`mlx_lm.convert` tool supports **2, 3, 4, 5, 6, and 8-bit** affine quantization, where groups of elements (default group
size 64, configurable to 32 or 128) share a scale and bias. The command is straightforward:

```bash
mlx_lm.convert --hf-path mistralai/Mistral-7B-Instruct-v0.3 -q --q-bits 4 --q-group-size 64
```

Beyond standard affine quantization, MLX supports three advanced formats. **MXFP4** (Microscaling FP4) uses E2M1 format
with fixed group size 32 — this is what GPT-OSS ships with. **DWQ (Distillation-Weighted Quantization)** is Apple's
technique where a 4-bit DWQ model achieves quality comparable to a 6-bit or 8-bit standard quantization; look for repos
ending in `-4bit-DWQ`. **QAT (Quantization-Aware Training)** models like Gemma 3 QAT are trained with quantization built
into the training loop, preserving quality at int4.

Mixed quantization is available via `--quant-predicate` with presets like `mixed_2_6`, `mixed_3_6`, and `mixed_4_6`. The
`mixed_4_6` preset keeps critical layers (attention value projections, down projections, first/last layers) at 6-bit
while compressing everything else to 4-bit — a meaningful quality improvement over uniform 4-bit with minimal size
increase.

The key difference from GGUF: MLX offers fewer quantization tiers but compensates with DWQ and MXFP4. GGUF's K-quant
system automatically assigns different precision to attention vs feed-forward layers and offers importance-matrix (
imatrix) quantization for superior quality-per-bit at very low bit rates. For MoE models specifically, GGUF's per-layer
sensitivity handling currently outperforms MLX's uniform approach at low bit widths.

---

## mlx-lm v0.31.1 and the daemon tooling stack

**mlx-lm** (version 0.31.1, released March 11, 2026) is the authoritative inference and fine-tuning library. Install
with `pip install mlx-lm`. It provides a built-in **OpenAI-compatible server**:

```bash
mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --port 8080
```

This serves `POST /v1/chat/completions` and `GET /v1/models`, usable with any OpenAI SDK client. However,
`mlx_lm.server` has critical limitations for daemon use: **no `--max-kv-size` support** in server mode means unbounded
KV cache growth, which has caused kernel panics on systems with ≤128GB. The default reply limit is 500 tokens. It lacks
continuous batching — requests are processed sequentially.

**LM Studio v0.4.6** with its MIT-licensed `mlx-engine` is the more production-ready option for daemon use. It adds
continuous batching (since v0.4.2), JIT model loading (models load on first request), configurable TTL (auto-unload
after N seconds idle), auto-eviction of previous models, speculative decoding, structured JSON output, and tool calling
with MCP support. Measured throughput: **~80 tok/s vs ~35 tok/s for Ollama** on typical 7B 4-bit models. The headless
daemon is configured via `lms`:

```bash
curl -fsSL https://lmstudio.ai/install.sh | bash
lms daemon up
lms get mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit
lms server start
```

Set JIT TTL to 300 seconds for auto-unloading idle models, and enable auto-evict so loading a new model cleanly replaces
the previous one. The SDK provides both Python (`pip install lmstudio`) and JavaScript interfaces with full model
lifecycle control.

**Ollama does not support MLX models as of March 2026.** An experimental MLX runner exists in the codebase (
`x/mlxrunner/`), but it hasn't shipped in a stable release. For now, Ollama remains GGUF-only. Alternative MLX servers
include **FastMLX** and **mlx-openai-server** (FastAPI-based with multi-model support).

On memory behavior: MLX uses a **pool allocator that does not return memory to the OS** when arrays are deleted. Freed
memory is recycled within MLX's pool for future allocations, but `Activity Monitor` will show it as consumed. There is
no `torch.mps.empty_cache()` equivalent. On macOS 15+, mlx-lm wires ~75% of RAM to prevent swapping, which is good for
performance but dangerous if KV cache grows unchecked. The rule of thumb: **model should use ≤75% of total RAM**,
leaving headroom for OS, KV cache, and the embedding model.

---

## Fine-tuning on Apple Silicon is practical and well-supported

MLX LoRA fine-tuning via `mlx_lm.lora` supports LoRA, DoRA, and full fine-tuning. **QLoRA activates automatically** when
you point `--model` at a quantized model — no extra flags needed. The memory savings are dramatic: a 7B model goes
from ~28GB (full precision) to **~6–8GB (QLoRA with 4-bit base)**. A 14B QLoRA fit requires ~10–12GB, well within the M3
Max 64GB envelope.

```bash
pip install "mlx-lm[train]"
mlx_lm.lora \
    --model mlx-community/Qwen2.5-Coder-14B-Instruct-4bit \
    --train --data ./training_data \
    --iters 1000 --batch-size 4 --num-layers 16 \
    --learning-rate 1e-5 --mask-prompt
```

Training data uses JSONL chat format, auto-detected from keys:

```jsonl
{"messages": [{"role": "system", "content": "You are a knowledge capture assistant."}, {"role": "user", "content": "Summarize this terminal session."}, {"role": "assistant", "content": "The session involved debugging a Python import error..."}]}
```

Files must be named `train.jsonl`, `valid.jsonl`, and `test.jsonl` in a single directory. The chat format automatically
applies the model's HuggingFace chat template. Use `--mask-prompt` to compute loss only on assistant responses.

**Incremental training is fully supported.** Resume from a checkpoint with
`--resume-adapter-file ./adapters/adapters.safetensors`, point to new data, and continue. Multi-stage domain adaptation
works by chaining resumptions with progressively specialized datasets. Merge adapters permanently with `mlx_lm.fuse`, or
use them dynamically at inference time with `--adapter-path`.

Realistic time estimates for **M3 Max 64GB**: a 7B QLoRA run of 1,000 iterations completes in **~10–20 minutes**. A 14B
QLoRA takes **~25–45 minutes**. On the M5 Max 128GB ceiling, expect ~20–30% faster training from higher memory
bandwidth. Fine-tuning a 30B-A3B MoE in 4-bit QLoRA is feasible on 64GB with batch-size=1 and `--grad-checkpoint`
enabled, but comfortable on 128GB at batch-size=2–4. A 70B QLoRA would require 128GB and remains tight.

Quality comparison from controlled benchmarks: LoRA validation loss **1.530** vs QLoRA validation loss **1.544** — the
difference is **negligible at ~0.9%**, confirming QLoRA is production-viable. Documented success stories include
text-to-SQL fine-tuning of Mistral-7B (validation loss dropped from 2.66 to 1.23 in under 10 minutes on M3),
function-calling adaptation of Phi-3 on an M1 Air (500 iterations, 10 minutes, 7.8GB peak), and enterprise adoption by
teams at Apple, IBM, Bosch, and Daimler Truck.

---

## MLX outperforms llama.cpp in throughput but trails on long-context prefill

The most rigorous comparison comes from the **vllm-mlx paper** (arXiv 2601.19139, January 2026), testing on M4 Max 128GB
across 10+ models from 0.6B to 30B parameters. MLX achieved **21% to 87% higher throughput** than llama.cpp across all
tested models, with the advantage most pronounced on smaller models (1.87x on Qwen3-0.6B) and still significant on large
MoE models (1.43x on Nemotron-30B-A3B). Peak throughput reached **525 tok/s** on the smallest model.

The academic benchmark from October 2025 (arXiv 2511.05502) on M2 Ultra 192GB measured steady-state decode at **~230
tok/s for MLX vs ~150 tok/s for llama.cpp** and ~35 tok/s for Ollama. MLX achieved >90% GPU utilization with median
per-token latency of 5–7ms.

Hardware-specific numbers for your target machines:

| Model                 | Hardware    | MLX tok/s (generation) | Notes                              |
|-----------------------|-------------|------------------------|------------------------------------|
| Qwen3-30B-A3B 4-bit   | M4 Max      | **87.6 tok/s**         | MoE, 17.3GB memory                 |
| Qwen3-30B-A3B 4-bit   | M3 Ultra    | 76.3 tok/s             |                                    |
| Qwen3.5-35B-A3B 4-bit | M4 Max      | **60–70 tok/s**        | Hybrid attention                   |
| Qwen3-8B 4-bit        | M4 Max      | ~50–60 tok/s           | 5.6GB memory                       |
| Qwen3-14B 4-bit       | M5 (24GB)   | **34.3 tok/s**         | M5 Max will be faster              |
| 8B model              | M5 Max 36GB | **61.6 tok/s**         | Early M5 Max data                  |
| Llama 3.1 8B Q4       | M3 Max 64GB | 23.4 tok/s             | vs 32 tok/s llama.cpp w/flash attn |

The M5 Max delivers **3.3–4.1x faster time-to-first-token** (TTFT) versus M4 thanks to Neural Accelerators, plus 19–27%
faster generation from higher memory bandwidth (estimated ~200 GB/s vs 153 GB/s on M4 Max). For your M3 Max floor,
expect generation performance roughly 15–20% below the M4 Max numbers.

**Where llama.cpp still wins**: at long contexts (8K+ tokens) with flash attention enabled, llama.cpp's generation speed
jumps dramatically (e.g., 9.4 → 32 tok/s on Llama 3.1 8B at 32K context). MLX lacks native flash attention — a community
PagedAttention implementation shows ~77% throughput improvements but isn't merged into core MLX. For short-output
workloads like RAG queries and tool-calling where prefill dominates, this gap matters. MLX prefill can consume **94% of
total inference time** at 8.5K context.

MoE models run well on MLX. The Qwen3-30B-A3B architecture (128 experts, 8 active per token) is a sweet spot: all 17GB
of weights live in memory, but only 3B activate per token, yielding "3B speed with 30B knowledge." On M3 Max 64GB, this
leaves ~45GB for OS, KV cache, and embedding model — comfortable headroom.

---

## MLX embedding models complete the RAG stack

The **mlx-embeddings** library (by Blaizzy/Prince Canuma, `pip install mlx-embeddings`) is the primary tool, supporting
BERT, ModernBERT, Qwen3, and Llama-based embedding architectures plus reranking.

For the knowledge base daemon, the recommended embedding model is **`mlx-community/Qwen3-Embedding-4B-4bit-DWQ`** — it
delivers **~18,000 tokens/sec** on M2 Max while producing 2560-dimensional embeddings that rank #1 on MTEB multilingual
benchmarks. If memory is tight, `mlx-community/all-MiniLM-L6-v2-4bit` (113K+ monthly downloads, ~30–50MB memory, 384
dimensions) runs at negligible resource cost. For maximum quality, `mlx-community/Qwen3-Embedding-8B-4bit-DWQ` produces
4096-dimensional embeddings at ~11,000 tokens/sec.

```python
from mlx_embeddings import load, generate
model, processor = load("mlx-community/Qwen3-Embedding-4B-4bit-DWQ")
output = generate(model, processor, texts=["terminal session: git rebase main..."])
embeddings = output.text_embeds  # normalized, ready for vector store
```

For serving both LLM and embeddings via a single API, **vllm-mlx** provides an OpenAI-compatible endpoint:

```bash
vllm-mlx serve mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit \
  --embedding-model mlx-community/Qwen3-Embedding-4B-4bit-DWQ
```

MLX rerankers are also available: the **Qwen3-Reranker** series (0.6B/4B/8B) provides cross-encoder reranking natively
through mlx-embeddings, enabling a complete two-stage retrieval pipeline (embed → retrieve → rerank) running entirely on
Apple GPU. The `mlx-community/Qwen3-Reranker-0.6B-mxfp8` model adds minimal overhead while significantly improving
retrieval precision.

Memory budget on M3 Max 64GB running the full stack: Qwen3-Coder-30B-A3B at 4-bit (~17GB) + Qwen3-Embedding-4B at
4-bit (~2.5GB) + Qwen3-Reranker-0.6B (~0.5GB) + OS overhead (~8GB) = **~28GB**, leaving ~36GB for KV cache and headroom.
On M5 Max 128GB, you could upgrade the inference model to a 70B class and run everything simultaneously.

---

## Critical limitations and practical gotchas

**Flash attention is absent.** This is MLX's most significant gap. The core framework uses optimized
`scaled_dot_product_attention` but lacks IO-aware flash attention. At context lengths beyond 8K tokens, llama.cpp with
`-fa` flag pulls ahead on generation speed. A community PagedAttention kernel shows promising results (~77% throughput
improvement) but isn't in the main codebase. For the daemon, cap context with `--max-kv-size` in CLI mode, and implement
context windowing in your application layer.

**The memory pool never releases.** MLX's allocator recycles freed memory internally but never returns it to macOS. Over
long daemon runtimes, memory pressure can build. Mitigation: use LM Studio's TTL-based auto-unloading, or implement
periodic process restarts. Monitor with `mx.metal.get_memory_info()`.

**bf16 weights penalize M1/M2 hardware.** MLX models default to bf16, which M1 and M2 chips don't support natively. On
M3 Max (which does support bf16) this isn't an issue, but if testing on older hardware, convert with
`mlx_lm.convert --dtype float16`.

**Prompt caching is broken for some architectures.** Hybrid attention models (gated delta-net in Qwen3.5, sliding window
attention) have broken prompt caching in the current mlx-engine, causing full prefill reprocessing every conversation
turn. This is actively being fixed but matters for long multi-turn conversations.

**KV cache grows without bound in server mode.** The `mlx_lm.server` doesn't support `--max-kv-size`, and unbounded
growth has triggered kernel panics on ≤128GB machines. Use LM Studio's daemon or implement your own context length
limits if using the raw server.

**Quantization granularity is coarser than GGUF.** You get 2/3/4/5/6/8-bit plus mixed presets, but GGUF offers
Q4_K_S/M/L, IQ4_XS, and importance-matrix quantization that squeezes more quality per bit. For MoE models at very low
bit rates, GGUF's per-layer sensitivity handling currently produces better results.

---

## Conclusion: recommended daemon configuration

For the M3 Max 64GB floor, the optimal daemon stack is **LM Studio headless** (`lms daemon up`) running *
*Qwen3-Coder-30B-A3B-Instruct-4bit** as the primary inference model (~17GB, ~60–80 tok/s generation), *
*Qwen3-Embedding-4B-4bit-DWQ** for RAG embeddings (~2.5GB, ~18K tokens/sec), and **Qwen3-Reranker-0.6B-mxfp8** for
retrieval reranking (~0.5GB). Total memory: ~28GB, leaving comfortable headroom. Set JIT TTL to 300 seconds and enable
auto-evict.

For the M5 Max 128GB ceiling, upgrade the inference model to **Qwen3.5-397B-A17B-4bit** (~62GB, the full Qwen3.5
flagship MoE) or **Llama-3.3-70B-Instruct-4bit** (~40GB), with the same embedding and reranker stack. The M5's Neural
Accelerators will cut TTFT by 3–4x compared to the M3 Max.

Fine-tuning for domain adaptation should use QLoRA on the 14B Qwen2.5-Coder-Instruct (~10–12GB training memory, ~25–45
minutes for 1K iterations), with incremental adapter training via `--resume-adapter-file` as the knowledge base grows.
The entire MLX ecosystem — inference, embeddings, reranking, and fine-tuning — runs natively on Apple Silicon without
any GGUF dependency.