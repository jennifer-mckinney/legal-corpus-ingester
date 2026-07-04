# LIB-ARCH — architecture reference
loads: on-trigger
scope: project
xref: [[LIB-STACK]] [[LIB-TEST]] [[LIB-PRINCIPLES]] [[.claude/CLAUDE.md]]

## module-tree

```
src/legal_corpus_ingester/
├── fetchers/       # Per-source fetch modules (HTTP + PDF + local file)
├── cleaners/       # HTML/PDF/XML/plaintext normalization (trafilatura, pdfminer.six, lxml)
├── chunkers/       # Section-aware + plain chunkers (vendored from terms-analysis)
├── embedders/      # LocalAI Apertus-8B client (thin httpx wrapper)
├── publishers/     # filesystem / terms-analysis / tarball output targets
├── pipeline/       # orchestrator + state machine + manifest writer + retention policy
└── provenance/     # tracker + license_audit + manifest writer
```

## interface-contracts

Each stage is behind a Python `Protocol`. Implementations are swappable without touching orchestrator code.

### Fetcher
```python
class Fetcher(Protocol):
    def fetch(self, source_id: str, config: SourceConfig) -> FetchResult: ...
```
Implementations: `HttpFetcher`, `PdfFetcher`, `LocalFileFetcher`
Failure contract: raises `FetchError` on non-200 / network failure; orchestrator catches + logs + skips source

### Cleaner
```python
class Cleaner(Protocol):
    def clean(self, raw: FetchResult) -> CleanedDocument: ...
```
Implementations: `HtmlCleaner` (trafilatura), `PdfCleaner` (pdfminer.six), `XmlCleaner` (lxml), `PlaintextCleaner`
Dispatch: `content_type` field on `FetchResult` selects cleaner

### Chunker
```python
class Chunker(Protocol):
    def chunk(self, doc: CleanedDocument) -> list[Chunk]: ...
```
Implementations: `SectionAwareChunker` (statute section headers), `PlainChunker` (sliding window)
Parity: output snapshots compared against terms-analysis chunker via syrupy (ADR-003)

### Embedder
```python
class Embedder(Protocol):
    def embed(self, chunks: list[Chunk]) -> list[EmbeddedChunk]: ...
```
Implementation: `LocalAIEmbedder` in `embedders/localai_client.py`
Model: Apertus-8B (multilingual legal); model SHA pinned in MANIFEST (ADR-008)
Failure contract: raises `EmbedError` on HTTP failure; orchestrator halts run (embeddings are load-bearing)

### Publisher
```python
class Publisher(Protocol):
    def publish(self, corpus: Corpus, target: PublishTarget) -> PublishReceipt: ...
```
Implementations: `FilesystemPublisher`, `TermsAnalysisPublisher`, `TarballPublisher`
Atomicity: write to `out/<version>.tmp/`, rename to `out/<version>/`, flip `out/current` symlink (ADR-004)

### ProvenanceTracker
```python
class ProvenanceTracker(Protocol):
    def record(
        self,
        source_id: str,
        result: FetchResult,
        chunks: list[Chunk],
    ) -> ProvenanceRecord: ...
```
Implementation: `DefaultProvenanceTracker` in `provenance/tracker.py`
Writes one `ProvenanceRecord` per source into MANIFEST under `sources[]`

## data-types

### FetchResult
```python
@dataclass
class FetchResult:
    source_id: str
    content: bytes
    http_status: int
    fetched_at: datetime
    content_type: str        # MIME type from Content-Type header or sniffed
    url: str
```

### CleanedDocument
```python
@dataclass
class CleanedDocument:
    source_id: str
    text: str
    metadata: dict[str, Any]
    license_spdx: str        # e.g. "CC-BY-4.0", "OGL-UK-3.0", "public-domain"
    license_sha256: str      # SHA256 of the license text fetched at clean time
```

### CitationTuple
```python
@dataclass
class CitationTuple:
    jurisdiction: str        # e.g. "EU", "US-CA", "PIPEDA"
    instrument: str          # e.g. "GDPR", "CCPA"
    short_label: str         # e.g. "GDPR Art.6(1)"
    unit: str                # e.g. "Article 6, Paragraph 1"
    eli_uri: str | None      # ELI URI for EUR-Lex sources
    us_lii_url: str | None   # LII URL for US federal/state sources
    license: str             # SPDX expression
    attribution: str         # Required attribution string per license
```

### Chunk
```python
@dataclass
class Chunk:
    source_id: str
    text: str
    citation: CitationTuple
    chunk_index: int
    chunker_version: str     # semver+gitsha — e.g. "1.0.0+abc1234"
```

### EmbeddedChunk
```python
@dataclass
class EmbeddedChunk(Chunk):
    embedding: list[float]
    embedder_model: str      # e.g. "Apertus-8B"
    embedder_revision: str   # git SHA of model weights
```

### Corpus
```python
@dataclass
class Corpus:
    version: str                         # calver e.g. "2026.07.01"
    generated_at: datetime
    chunks: list[EmbeddedChunk]
    provenance: list[ProvenanceRecord]
```

### ProvenanceRecord
```python
@dataclass
class ProvenanceRecord:
    source_id: str
    revision_date: str        # YYYY-MM-DD — last statute revision date
    approval_ref: str         # SHA256 of APPROVAL.yaml at time of ingest
    chunk_count: int
    sha256: str               # SHA256 of raw fetched bytes
    license_spdx: str
    license_sha256: str
    http_status: int
    fetched_at: datetime
    byte_count: int
```

## manifest-schema

ADR-008. Written to `out/<version>/MANIFEST.yaml` by `ProvenanceTracker`.

```yaml
corpus_version: "YYYY.MM.PATCH"    # calver
generated_at: "ISO8601"
chunker_version: "semver+gitsha"   # e.g. "1.0.0+abc1234"
embedder_model: "Apertus-8B"
embedder_revision: "git-sha"       # model weights SHA
sources:
  - id: "eurlex"
    revision_date: "YYYY-MM-DD"
    approval_ref: "APPROVAL.yaml sha"
    chunk_count: 1234
    sha256: "hex"
    license_spdx: "CC-BY-4.0"
    license_sha256: "hex"
    http_status: 200
    fetched_at: "ISO8601"
    byte_count: 123456
```

Consumer validation: `terms-analysis` `legal_kb.py` verifies all required MANIFEST fields on load;
`embedder_revision` mismatch → HTTP 503 (ADR-014)

## publish-mechanism

ADR-004 + ADR-012.

1. Embedder writes chunks to `out/<version>.tmp/` (partial, not yet usable)
2. Publisher writes MANIFEST.yaml to `out/<version>.tmp/MANIFEST.yaml`
3. `os.rename("out/<version>.tmp", "out/<version>")` — atomic on POSIX
4. `os.symlink("out/<version>", "out/current.new")` + `os.rename("out/current.new", "out/current")` — atomic symlink flip
5. SIGHUP sent to terms-analysis FastAPI process (reload legal-KB in-place)
6. Fallback: `POST /reload` to terms-analysis API if SIGHUP fails (process not found)
7. Consumer verifies MANIFEST fields on load (ADR-014); mismatch → raises `CorpusVersionError`

Old bundles retained per retention policy (default: keep last 3 versions, configurable via `config/pipeline.yaml`)

## data-flow

```
config/sources/*.yaml
       ↓ SourceConfig (per-source fetch config: url, auth, content_type, license)
   Fetcher.fetch()
       ↓ FetchResult (raw bytes, http_status, content_type, fetched_at)
   Cleaner.clean()
       ↓ CleanedDocument (normalized text, metadata, license_spdx, license_sha256)
   Chunker.chunk()
       ↓ list[Chunk] (citation-tagged text segments, chunk_index, chunker_version)
   Embedder.embed()
       ↓ list[EmbeddedChunk] (Chunk + embedding vector, embedder_model, embedder_revision)
   ProvenanceTracker.record()
       ↓ ProvenanceRecord (source audit trail: sha256, byte_count, approval_ref, etc.)
   Publisher.publish()
       ↓ out/YYYY.MM.PATCH/ bundle (EmbeddedChunks + MANIFEST.yaml)
       ↓ out/current symlink flipped atomically
       ↓ SIGHUP / POST /reload → terms-analysis FastAPI reloads legal-KB
```

## failure-modes

| id | failure | behavior |
|----|---------|----------|
| F1 | Upstream HTTP non-200 | `FetchError` raised; orchestrator logs + skips source; other sources continue |
| F2 | Network timeout | `FetchError` with timeout detail; same skip behavior as F1 |
| F3 | Cleaner parse error | `CleanError` raised; source skipped; logged with raw byte preview |
| F4 | LocalAI embed endpoint unreachable | `EmbedError` raised; entire run halted (embeddings load-bearing) |
| F5 | LocalAI returns wrong vector dimension | `EmbedDimensionError`; entire run halted; MANIFEST not written |
| F6 | embedder_revision mismatch on consumer load | Consumer raises `CorpusVersionError`; terms-analysis serves stale corpus until resolved |
| F7 | Atomic rename fails (cross-device) | `PublishError`; partial bundle cleaned up; run marked FAILED in pipeline state |
| F8 | APPROVAL.yaml missing for source | Source blocked; `ApprovalMissingError`; not fetched until approval registered |
| F9 | License not in approved SPDX list | `LicenseViolationError`; source blocked (HR1 compliance) |
| F10 | Corpus version conflict (version already exists) | `VersionConflictError`; run halted; must bump calver |
