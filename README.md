# Materials Search Engine

A coverage-driven literature search engine for materials science. Given a research question in natural language, it decomposes the problem into a term matrix, runs parallel keyword + citation searches across Scopus and OpenAlex, scores each paper for relevance and coverage, and iteratively fills coverage gaps until the literature is comprehensively mapped.

> **Why "coverage-driven"?** Naive LLM search agents suffer from *topic collapse*: round 1 finds one class of papers → the agent extracts similar terms → round 2 searches even more similar papers → relevance rises while coverage narrows. This engine instead asks "what is still *missing*?" and drives each round by gaps in the coverage map.

## Features

- **Coverage-driven search** — a research question is decomposed into a term matrix, expanded into a query population, and iteratively searched with gap-driven rounds (asks "what is still missing", not "what is similar").
- **Dual-channel recall** — keyword search (Scopus) + citation graph expansion (OpenAlex forward/backward/co-citation) + Foundational Recovery (citation + keyword channels for classic/early papers).
- **Knowledge Extractor** — extracts searchable knowledge from papers: causal mechanisms (cause→mechanism→effect), strategy routes, historical terms, and generalized search hypotheses (Paper Evidence → Hypothesis → Query).
- **Knowledge Base** — persists extracted knowledge with source traceability, and generates knowledge-derived historical queries (replacing hardcoded route queries).
- **Structured labeling** — relevance score, technical route, evidence type, information gain per paper.
- **Coverage map** — papers clustered into routes; gaps drive the next round.
- **MMR diversity re-ranking** — avoids top-k being dominated by similar papers.
- **CSV export** — scored results with route/category/info-gain columns.

## Architecture

```
Research question
      │
      ▼
Term matrix (8 dimensions)
      │
      ▼
Query population (8+ queries, return-tracked)
      │
      ▼
┌──────────────────────────┬──────────────────────────┐
│  Keyword channel         │  Citation channel        │
│  (Scopus advanced search)│  (OpenAlex fwd/back/co-  │
│                          │   citation via seed DOI) │
└──────────────────────────┴──────────────────────────┘
      │
      ▼
Structured labeling (relevance + route + info_gain)
      │
      ▼
Coverage map (cluster routes → identify gaps)
      │
      ▼
Gap-driven next round  ──────────────┐
      ▲                              │
      └──────────────────────────────┘
```

## Install

```bash
# Python 3.11+
python -m venv .venv
# Windows: .venv\Scripts\activate   |  Linux/macOS: source .venv/bin/activate

pip install -e .
```

`pip install -e .` installs the project plus dependencies:
- `cloakbrowser` — stealth Chromium for Scopus (bot-detection evasion)
- `beautifulsoup4`, `lxml` — HTML parsing
- `httpx` — HTTP client (DeepSeek / Ollama / OpenAlex)

## Prerequisites

| Service | Purpose | Required? |
|---------|---------|-----------|
| **Scopus** institutional access | keyword search + export | Yes (via institutional VPN/SSO) |
| **DeepSeek API key** (or local Ollama) | query generation + paper scoring | Yes |
| **OpenAlex** | citation tracking | Optional (`--citations`) |
| **Ollama** (local model) | free alternative to DeepSeek | Optional |

## Usage

### 1. First-time Scopus login

```bash
python -m search_engine login
```

A browser window opens. Sign in through your institution's SSO, then press Enter.
The session is saved locally (never committed — see `.gitignore`).

### 2. Single search

```bash
python -m search_engine search "TITLE-ABS-KEY(photocuring AND composite) AND DOCTYPE(ar)" --csv results.csv
```

### 3. Full coverage-driven search (natural language)

```bash
# DeepSeek
python -m search_engine generate "polymerization shrinkage of photocurable composites" \
    --provider deepseek --search --filter --citations \
    --threshold 70 --top-k 10 --csv results.csv

# Local Ollama (free)
python -m search_engine generate "polymerization shrinkage of photocurable composites" \
    --provider ollama --model qwen2.5:7b --search --filter
```

Set the DeepSeek key via environment variable (recommended):

```bash
# Windows
set DEEPSEEK_API_KEY=sk-...

# Linux/macOS
export DEEPSEEK_API_KEY=sk-...
```

Or pass it inline: `--api-key sk-...`

### 4. Other commands

```bash
python -m search_engine check      # verify Scopus session
python -m search_engine stats      # cache statistics
python -m search_engine compile --keywords a,b --synonyms "a:x,y"  # intent → query
```

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--provider` | `deepseek` | `deepseek` or `ollama` |
| `--model` | `deepseek-chat` / `qwen2.5:7b` | model ID |
| `--threshold` | `70` | relevance cutoff (0-100) |
| `--top-k` | `20` | max papers to keep |
| `--citations` | off | enable OpenAlex citation tracking |
| `--mailto` | — | OpenAlex polite-pool email (optional) |

## Privacy & Security

- **API keys** are passed via environment variable or CLI flag — never hardcoded.
- **`data/` is git-ignored** — it contains your Scopus session (`state.json`), cache, and exported results.
- The `state.json` session cookie grants access to your institutional Scopus — treat it like a password and never commit or share it.

## Disclaimer

This tool automates access to Scopus for **your own institutional research use**. Respect Elsevier's terms of service and your institution's acceptable-use policy. Do not use it to scrape or redistribute copyrighted content.

## Project Layout

```
search_engine/
├── engine.py              # Scopus search + export REST API
├── models.py              # Paper, ScoredPaper, TermMatrix, KnowledgeRecord, ...
├── compiler.py            # structured intent → Scopus query syntax
├── cache.py               # SQLite cache + search log
├── csv_exporter.py        # CSV export (scored results)
├── llm.py                 # DeepSeek + Ollama backends (JSON mode + truncation detection)
├── knowledge.py           # domain term knowledge (photocuring/ML/motor)
├── relevance.py           # recall-first pre-filter + structured scoring
├── term_matrix.py         # question → per-dimension term matrix (two-stage routes)
├── query_population.py    # query pool (coverage + exploration)
├── coverage_map.py        # route clustering + gap identification
├── iterative_searcher.py  # coverage-driven iterative search (dual-channel)
├── citation_tracker.py    # OpenAlex forward/backward/co-citation + rate-limit guard
├── knowledge_extractor.py # paper → searchable knowledge (mechanisms/hypotheses)
├── knowledge_base.py      # persist knowledge + generate historical queries
├── query_relaxer.py       # progressive query relaxation (backlogged)
├── foundational_recovery.py # citation+keyword recall for classic papers
├── evaluator.py           # benchmark coverage evaluation
├── mmr.py                 # MMR diversity re-ranking
├── query_generator.py     # single-shot query generator (legacy)
└── cli.py                 # CLI entry point
```

## Roadmap

### Phase 0 — Search infrastructure ✅
- [x] Scopus search + export
- [x] LLM query generation (DeepSeek / Ollama)
- [x] Iterative search + relevance filtering
- [x] Coverage-driven architecture (term matrix / query population / coverage map)
- [x] OpenAlex citation tracking (dual channel)
- [x] Foundational Recovery (citation + keyword recall)
- [x] MMR diversity re-ranking
- [x] Production reliability (per-dimension term extraction, rate-limit guards)

### Phase 1 — Knowledge Extractor ✅ (A/B pending quota)
- [x] Knowledge Extractor (causal mechanisms, generalized hypotheses)
- [x] Knowledge Base (persistence + source traceability)
- [x] Knowledge-derived historical query generation
- [x] Mechanism-driven new-literature recall (validated: 8 unique new relevant papers)
- [ ] Final A/B: Knowledge-driven vs Baseline expansion (equal budget, awaiting OpenAlex quota)

### Phase 2 — Autonomous discovery (planned)
- [ ] Novelty / unknown-route discovery
- [ ] Search → Learn → Search closed loop

### Later
- [ ] Benchmark set (pilot: 5 photocuring questions)
- [ ] Evaluation framework (relevance / coverage / evidence quality / cost)
- [ ] Independent statistical audit (recall lower bound)
- [ ] Explore-exploit balance (bandit)
