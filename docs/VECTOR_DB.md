# Vector-DB backend — choosing one

Experience Memory (the distilled-lessons store) is **pluggable**. The choice
of vector DB doesn't change anything in the rest of the system — the same
`MemoryStore` interface; different storage + retrieval engine underneath.

## TL;DR

```bash
# Default — nothing to do. SQLite on disk + hash embedder. Works offline.
# (Use ARK_API_KEY to upgrade embeddings to BytePlus ModelArk's model automatically.)

# Local with a richer DB:
export APRNTC_VECTOR_DB_URL="chroma:///./aprntc_chroma"
pip install 'aprntc[chroma]'

# BytePlus VikingDB (cloud, server-side embedding):
export APRNTC_VECTOR_DB_URL="byteplus://my-collection/my-index"
export VIKINGDB_AK=… VIKINGDB_SK=…
pip install 'aprntc[byteplus]'

# Pinecone (cloud, serverless):
export APRNTC_VECTOR_DB_URL="pinecone://aprntc-lessons"
export PINECONE_API_KEY=…
pip install 'aprntc[pinecone]'

# AWS OpenSearch (k-NN plugin):
export APRNTC_VECTOR_DB_URL="aws://search-mydomain.us-east-1.es.amazonaws.com/aprntc-lessons"
export AWS_OPENSEARCH_USER=… AWS_OPENSEARCH_PASSWORD=…
pip install 'aprntc[aws]'
```

## How the choice flows through

```
APRNTC_VECTOR_DB_URL
   │
   ▼
make_memory_store(url)        ← dispatches on URL scheme
   │
   ▼
LocalMemoryStore | ChromaMemoryStore | VikingDBMemoryStore
| PineconeMemoryStore | AwsOpenSearchMemoryStore
   │ (all implement the same MemoryStore Protocol)
   ▼
AppState.memory_search        ← wired into /api/lessons + the distiller
```

`AppState.from_env()` reads `APRNTC_VECTOR_DB_URL` and calls `make_memory_store()`.
If unset, the default is `local:///./aprntc_memory.db` (SQLite, no creds needed).

## Per-backend details

### 1. Local (default) — `local://path/to/file.db`

- **Storage:** SQLite (one file, easy to back up).
- **Vectors:** computed client-side via an `Embedder`, persisted as JSON columns.
- **Retrieval:** full-scan cosine + MMR diversification (fine to ~10k lessons).
- **Embedder:** picked by `default_embedder()` —
  - `ARK_API_KEY` set → **ModelArk embedder** (real semantic; 2048-dim doubao).
  - `OPENAI_API_KEY` set → OpenAI embedder.
  - Neither set → `HashEmbedder` (deterministic, low semantic quality, **works offline**).
- **When to use:** development, single-host deploys, ≤10k lessons, no cloud dep wanted.
- **When to outgrow:** retrieval latency starts mattering, lesson count > ~10k, or you
  need horizontal scale.

Plain paths (no scheme) also work: `APRNTC_VECTOR_DB_URL=./mem.db` → LocalMemoryStore.

### 2. Chroma — `chroma:///path/to/dir`

- **Extra:** `pip install 'aprntc[chroma]'` (ChromaDB).
- **Storage:** Chroma's persistent client (file-based, in-process).
- **Embeddings:** Chroma's built-in ONNX sentence-transformer (no API key needed).
  Higher quality than `HashEmbedder` without any cloud dep.
- **When to use:** local-only workflow but you want real semantic retrieval without
  burning ModelArk/OpenAI tokens on embedding.

### 3. BytePlus VikingDB — `byteplus://collection/index`

- **Extra:** `pip install 'aprntc[byteplus]'`.
- **Env:** `VIKINGDB_AK`, `VIKINGDB_SK`, region defaulted.
- **Storage:** managed cloud vector DB.
- **Embeddings:** server-side (skylark / doubao, 2048-dim) — you send text, the
  server computes vectors.
- **When to use:** you already pay for BytePlus and want a managed option; the
  rest of the project also uses ModelArk so credentials are unified.
- **Caveat (live finding, see STATUS.md Stage 5):** collections must be
  provisioned via the BytePlus Console; programmatic create isn't accepted.

### 4. Pinecone — `pinecone://index-name`

- **Extra:** `pip install 'aprntc[pinecone]'`.
- **Env:** `PINECONE_API_KEY` (required), `PINECONE_CLOUD`/`PINECONE_REGION` (defaults: `aws`/`us-east-1`).
- **Storage:** Pinecone serverless.
- **Embeddings:** BYO via the injected `Embedder`. Defaults to `default_embedder()`.
  Index dimension is set to match the embedder on first create.
- **When to use:** managed, low-latency, scale-to-zero pricing; want a non-AWS cloud option.

### 5. AWS OpenSearch — `aws://host:port/index`

- **Extra:** `pip install 'aprntc[aws]'`.
- **Env:** `AWS_OPENSEARCH_USER`/`AWS_OPENSEARCH_PASSWORD` (basic auth), `AWS_OPENSEARCH_USE_SSL`.
- **Storage:** OpenSearch with the k-NN plugin (managed AWS or self-hosted).
- **Embeddings:** BYO via the injected `Embedder`.
- **When to use:** you're on AWS and want vector search alongside your existing
  log / search infrastructure.

## Tenant isolation

When the dashboard runs in multi-tenant mode (B2), each tenant's memory URL is
namespaced automatically by `_tenant_scoped_url`:

| Base URL | Per-tenant URL |
|---|---|
| `local:///./mem.db` | `local:///mem.acme-co.db` |
| `chroma:///./aprntc_chroma` | `chroma:///./aprntc_chroma/acme-co` |
| `byteplus://col/idx` | `byteplus://acme-co_col/acme-co_idx` |
| `pinecone://lessons` | `pinecone://acme-co-lessons` |
| `aws://host/lessons` | `aws://host/acme-co-lessons` |

The tenant never sees another tenant's lessons even on a shared cloud backend.

## Embedders

Cloud backends that take BYO vectors (Pinecone, AWS) use an `Embedder` Protocol:

```python
from aprntc.memory import HashEmbedder, ModelArkEmbedder, OpenAIEmbedder, default_embedder

# Pick explicitly or let default_embedder() decide based on env:
embedder = ModelArkEmbedder()         # uses ARK_API_KEY
embedder = OpenAIEmbedder()           # uses OPENAI_API_KEY
embedder = HashEmbedder()             # offline, low quality
embedder = default_embedder()         # auto-detect from env

# Build the store with that embedder:
from aprntc.memory.pinecone_store import PineconeMemoryStore
store = PineconeMemoryStore(index_name="aprntc-lessons", embedder=embedder)
```

Custom Embedder? Implement two methods (`dim`, `embed(texts)`) — any class
matching the Protocol works.

## Migration path

Switching backends is non-destructive: the old store keeps its data. To migrate,
read lessons from the old backend and `upsert_lessons` into the new one (the
content-derived `lesson_id` makes the upsert idempotent — re-running is safe).

A `scripts/migrate_memory.py` helper is on the TODO list; for now the migration
is a few lines:

```python
from aprntc.memory import make_memory_store
old = make_memory_store("byteplus://my-collection")
new = make_memory_store("local:///./aprntc_memory.db")
new.upsert_lessons([h.lesson for h in old.retrieve(query="", k=10000, min_reward=0.0)])
```

## Choosing in two questions

1. **Do you need cloud-scale retrieval (>10k lessons or low-latency at scale)?**
   - **No** → Local (default) or Chroma. Done.
   - **Yes** → continue.
2. **Which cloud do you live in?**
   - BytePlus → VikingDB.
   - AWS → OpenSearch.
   - Anywhere else / multi-cloud → Pinecone.
