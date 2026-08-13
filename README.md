# Materials Search Engine

A coverage-driven literature search engine for materials science. Given a research question in natural language, it decomposes the problem into a term matrix, runs parallel keyword + citation searches across Scopus and OpenAlex, scores each paper for relevance and coverage, and iteratively fills coverage gaps until the literature is comprehensively mapped.

> **Why "coverage-driven"?** Naive LLM search agents suffer from *topic collapse*: round 1 finds one class of papers → the agent extracts similar terms → round 2 searches even more similar papers → relevance rises while coverage narrows. This engine instead asks "what is still *missing*?" and drives each round by gaps in the coverage map.

## Features

- **Term matrix decomposition** — a research question is split into 8 dimensions (material system, composition, mechanism, process, properties, application, failure modes, metrics) before any search happens.
- **Query population** — maintains a pool of queries, each scored by *new papers found*, *new routes discovered*, *duplicate rate*, and *cost* (not just relevance).
- **Dual-channel search** — keyword search (Scopus) + citation graph expansion (OpenAlex forward/backward/co-citation).
- **Structured labeling** — every paper gets a relevance score, a technical route, and an *information gain* score (how much new information it adds).
- **Coverage map** — papers are clustered into research routes; gaps drive the next round of queries.
- **Year weighting** — recent papers (≤3 years) are boosted, papers >5 years old penalized.
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
├── models.py              # Paper, ScoredPaper, TermMatrix, QueryEntry, ...
├── compiler.py            # structured intent → Scopus query syntax
├── cache.py               # SQLite cache + search log
├── csv_exporter.py        # CSV export (scored results)
├── llm.py                 # DeepSeek + Ollama backends
├── knowledge.py           # domain term knowledge (photocuring/ML/motor)
├── relevance.py           # pre-filter + structured scoring + year weighting
├── term_matrix.py         # question → 8-dimension term matrix
├── query_population.py    # query pool with return tracking
├── coverage_map.py        # route clustering + gap identification
├── iterative_searcher.py  # coverage-driven iterative search (dual-channel)
├── citation_tracker.py    # OpenAlex forward/backward/co-citation
├── query_generator.py     # single-shot query generator (legacy)
└── cli.py                 # CLI entry point
```

## Roadmap

- [x] Scopus search + export
- [x] LLM query generation (DeepSeek / Ollama)
- [x] Iterative search + relevance filtering
- [x] Coverage-driven architecture (term matrix / query population / coverage map)
- [x] OpenAlex citation tracking (dual channel)
- [ ] Explore-exploit balance (bandit) + MMR diversity re-ranking
- [ ] Benchmark set (pilot: 5 photocuring questions)
- [ ] Evaluation framework (relevance / coverage / evidence quality / cost)
