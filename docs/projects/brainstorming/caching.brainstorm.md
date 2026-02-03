# Semantic Response Caching Brainstorm

## Overview

Cache agent responses to avoid redundant API calls, reducing costs by 60-80% and improving response times. Support both exact-match caching (fast, hash-based) and semantic caching (finds similar prompts).

## Motivating Use Case

A developer iterates on a workflow during development. They run it 20 times with minor prompt tweaks. Each run calls 5 agents. Without caching: 100 API calls. With exact-match caching on stable agents: 40 API calls (60% reduction).

**Current behavior:**
```
# Run 1
conductor run workflow.yaml --input topic="AI safety"
# 5 API calls

# Run 2 (same input, minor prompt change in agent 3)
conductor run workflow.yaml --input topic="AI safety"
# 5 API calls (agents 1,2,4,5 identical to run 1)
```

**Desired behavior (with caching):**
```
# Run 2
conductor run workflow.yaml --input topic="AI safety"
# 1 API call (only agent 3 with changed prompt)
# Agents 1,2,4,5 served from cache
```

## Research: Industry Cache Hit Rates

Based on web research, production systems achieve:
- **70-85% cache hit rate** after 24 hours of operation
- **60-80% cost reduction** with aggressive caching
- **Response time: 2-3s → 50-100ms** for cache hits

## Design Decisions

### 1. Caching Granularity

| What to cache | Pros | Cons | Decision |
|---------------|------|------|----------|
| Full agent output | Simple, complete | Large storage | ✅ **Selected** |
| Parsed output only | Smaller storage | Loses raw response | Future option |
| Streaming chunks | Enables stream replay | Complex | Out of scope |

### 2. Cache Key Components

The cache key must uniquely identify an agent invocation:

```python
cache_key = hash(
    agent_name,          # Which agent
    rendered_prompt,     # Full prompt after template rendering
    model,               # Model affects output
    output_schema,       # Schema affects structured output
    tools,               # Tools affect behavior
    system_prompt,       # System prompt affects behavior
)
```

**Excluded from key**:
- `context` (already incorporated into rendered_prompt)
- `tokens_used` (output, not input)

### 3. Cache Storage Backend

| Backend | Pros | Cons | Decision |
|---------|------|------|----------|
| SQLite | Portable, zero-config | Single-process | ✅ **Default** |
| Redis | Fast, distributed | Requires server | Future option |
| Memory | Fastest, no I/O | Lost on restart | For testing |

### 4. Semantic Similarity (Optional Layer)

**Exact match** handles identical prompts. **Semantic caching** handles paraphrases:

```
"What is the capital of France?" → cached response
"France's capital city is?" → 95% similar → return cached response
```

| Approach | Pros | Cons | Decision |
|----------|------|------|----------|
| Embedding similarity | Finds paraphrases | Needs embedding model | ✅ **Optional** |
| LLM-based comparison | Accurate | Expensive | No |
| Keyword overlap | Simple | Inaccurate | No |

**Decision**: Exact-match first (v1), semantic as optional enhancement (v2).

### 5. Cache Invalidation

| Strategy | When to use |
|----------|-------------|
| TTL-based | Default, auto-expire after time |
| Manual | User clears cache explicitly |
| Version-based | Invalidate when workflow version changes |
| Content-aware | Detect prompt changes, invalidate affected |

**Decision**: TTL-based with manual override. Keep it simple.

## YAML Syntax

### Basic (defaults)
```yaml
workflow:
  name: my-workflow
  cache:
    enabled: true              # Default: false
    storage: sqlite            # sqlite | memory
    path: .conductor/cache/
    ttl_seconds: 86400         # 24 hours
```

### Full Configuration
```yaml
workflow:
  name: my-workflow
  cache:
    enabled: true
    storage: sqlite
    path: .conductor/cache/${workflow.name}.db
    ttl_seconds: 86400
    max_entries: 10000         # Prune oldest when exceeded
    semantic:
      enabled: true            # Enable semantic similarity
      threshold: 0.95          # Minimum similarity for match
      embedding_model: text-embedding-3-small
```

### Per-Agent Override
```yaml
agents:
  - name: always_fresh
    cache: false               # Bypass cache for this agent
    prompt: "..."

  - name: stable_classifier
    cache:
      ttl_seconds: 604800      # 7 days for stable agent
    prompt: "..."
```

## Implementation Components

### 1. Cache Key Generation

```python
import hashlib
import json

def generate_cache_key(
    agent_name: str,
    rendered_prompt: str,
    model: str,
    output_schema: dict | None,
    tools: list[str] | None,
    system_prompt: str | None,
) -> str:
    """Generate deterministic cache key."""
    key_data = {
        "agent": agent_name,
        "prompt": rendered_prompt,
        "model": model,
        "schema": output_schema,
        "tools": sorted(tools or []),
        "system": system_prompt,
    }
    key_json = json.dumps(key_data, sort_keys=True)
    return hashlib.sha256(key_json.encode()).hexdigest()[:32]
```

### 2. Cache Store Abstraction (`engine/cache.py`)

```python
@dataclass
class CacheEntry:
    key: str
    content: dict[str, Any]
    raw_response: str
    tokens_used: int | None
    model: str | None
    created_at: datetime
    expires_at: datetime
    hit_count: int = 0

class CacheStore(ABC):
    @abstractmethod
    async def get(self, key: str) -> CacheEntry | None: ...

    @abstractmethod
    async def set(self, key: str, entry: CacheEntry) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def clear(self) -> None: ...

    @abstractmethod
    async def stats(self) -> CacheStats: ...

class SQLiteCacheStore(CacheStore):
    def __init__(self, db_path: Path): ...
    # Implementation with aiosqlite

class MemoryCacheStore(CacheStore):
    def __init__(self, max_entries: int = 1000): ...
    # Implementation with dict + LRU eviction
```

### 3. Cache Integration in AgentExecutor

```python
class AgentExecutor:
    def __init__(self, provider, cache_store=None):
        self.provider = provider
        self.cache = cache_store

    async def execute(self, agent, context) -> AgentOutput:
        # Generate cache key
        cache_key = generate_cache_key(
            agent.name,
            rendered_prompt,
            agent.model,
            agent.output,
            agent.tools,
            agent.system_prompt,
        )

        # Check cache
        if self.cache and agent.cache_enabled:
            cached = await self.cache.get(cache_key)
            if cached and not cached.is_expired():
                self._log_cache_hit(agent.name)
                return AgentOutput(
                    content=cached.content,
                    raw_response=cached.raw_response,
                    tokens_used=0,  # No API call
                    model=cached.model,
                    from_cache=True,  # NEW field
                )

        # Cache miss - execute
        output = await self.provider.execute(...)

        # Store in cache
        if self.cache and agent.cache_enabled:
            await self.cache.set(cache_key, CacheEntry(
                key=cache_key,
                content=output.content,
                raw_response=output.raw_response,
                tokens_used=output.tokens_used,
                model=output.model,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(seconds=ttl),
            ))

        return output
```

### 4. Storage Schema (SQLite)

```sql
CREATE TABLE cache_entries (
    key TEXT PRIMARY KEY,
    content TEXT NOT NULL,           -- JSON
    raw_response TEXT,
    tokens_used INTEGER,
    model TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    hit_count INTEGER DEFAULT 0
);

CREATE INDEX idx_expires_at ON cache_entries(expires_at);
```

### 5. CLI Commands

```bash
# Show cache stats
conductor cache stats

# Clear cache
conductor cache clear
conductor cache clear --workflow my-workflow

# Prune expired entries
conductor cache prune

# Disable cache for a run
conductor run workflow.yaml --no-cache
```

## Output Format (Verbose Mode)

```
[1/5] 🤖 classifier (gpt-4o-mini)
    └─ ✓ [CACHE HIT] 0.05s
    → analyzer

[2/5] 🤖 analyzer (claude-sonnet-4)
    ├─ 🔧 web_search
    └─ ✓ 12.3s | 1,234 in / 567 out | $0.05
    → summarizer
```

### Cache Stats Summary

```
═══════════════════════════════════════════════════
Cache Performance:
  Hits:   4 (80%)
  Misses: 1 (20%)
  Saved:  ~$0.16 estimated

Cache Storage:
  Entries: 127
  Size:    2.3 MB
  Oldest:  2 hours ago
═══════════════════════════════════════════════════
```

## Files Affected

### New Files
- `src/conductor/engine/cache.py` - CacheStore and implementations
- `src/conductor/cli/cache.py` - CLI commands
- `tests/test_engine/test_cache.py` - Unit tests

### Modified Files
- `src/conductor/config/schema.py` - Add CacheConfig
- `src/conductor/executor/agent.py` - Integrate caching
- `src/conductor/engine/workflow.py` - Initialize cache store
- `src/conductor/cli/app.py` - Add cache commands
- `src/conductor/cli/run.py` - Add --no-cache flag
- `src/conductor/providers/base.py` - Add from_cache field to AgentOutput

## Open Questions

1. **Parallel group caching**: Cache individual agents or entire group result?

2. **For-each caching**: Cache per-item or aggregate? Items may have similar prompts.

3. **Context-dependent prompts**: If prompt includes `{{ prior_agent.output }}`, cache key changes every time. Should we normalize context?

4. **Semantic cache embedding cost**: Embedding calls also cost money. When is it worth it?

5. **Cache warming**: Pre-populate cache with expected prompts?

## Future Enhancements

- Semantic caching with embeddings
- Redis backend for distributed caching
- Cache prewarming for predictable workflows
- Cache sharing across similar workflows
- Integration with cost tracking (show savings)
- LRU eviction policy options
