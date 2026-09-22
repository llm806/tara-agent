# Tara Agent backend

The backend keeps HTTP transport, reproducible data access, and domain contracts separate.
Source TSV files are immutable inputs; runtime code reads only validated Parquet artifacts.

```text
src/tara_agent/
├── agent/        LangGraph request routing, analysis workflow, prompts, and resource metadata
├── api/          FastAPI application and HTTP schemas/routes
├── analysis/     transport-independent query and compute services
├── data/         source validation, preprocessing, manifest, and processed-data access
├── domain/       transport-independent shared contracts
├── mcp/          thin MCP server and tool adapters
├── observability/ unified Trace contracts, execution context, and recorder
├── persistence/  PostgreSQL engine and SQLAlchemy persistence models
└── config.py     environment-backed configuration
```

## Database

PostgreSQL stores chat sessions, messages, request traces, and nested trace spans. Start the
database from the repository root, then apply versioned migrations from this directory:

```powershell
docker compose up -d postgres
uv run alembic upgrade head
```

The application reads `TARA_DATABASE_URL` from the root `.env` and then `backend/.env`; the latter
takes precedence. Alembic is the only supported way to change shared database structure.

Each chat request atomically creates a user message, an assistant placeholder, and one Agent
trace. Successful and failed executions both persist ordered spans for request routing, planning,
tool execution, and answer generation. Streaming text is buffered in memory and committed at the
end of the run instead of writing every token to PostgreSQL.

## Preprocess data

Run the reproducible preprocessing pipeline through the project entrypoint:

```powershell
uv run tara-data
```

Use `--force` to rebuild and verify that Parquet checksums are reproducible:

```powershell
uv run tara-data --force
```

Outputs are atomically published below `data/processed/`:

```text
data/processed/
├── manifest.json
└── generation-<fingerprint>/
    ├── context.parquet
    ├── v4_asv_metadata.parquet
    ├── v4_abundance.parquet
    ├── v9_asv_metadata.parquet
    ├── v9_abundance.parquet
    └── validation.json
```

V4 and V9 remain independent. ASV metadata is separated from each wide abundance matrix so
taxonomy queries can scan a small file and abundance queries can project only the requested
sample columns. `manifest.json` records source hashes, schemas, shapes, coverage, artifact hashes,
processing time, and peak memory.

Query-format benchmarking is intentionally separate from preprocessing. It is a development task
that compares raw TSV with Parquet through Polars and DuckDB:

```powershell
uv run python benchmarks/query_formats.py
```

The script prints its JSON report to standard output. DuckDB is a development dependency and is
not required by preprocessing, the API, MCP, or the Agent runtime.

## Deterministic queries

`TaraQueryService` currently provides the first three MVP query capabilities:

- `find_samples`: filter samples by region, polar flag, depth, size fraction, and temperature.
- `get_sample_info`: return complete validated context for one sample ID.
- `find_taxa`: search taxonomy within one explicit marker and aggregate matching raw reads by
  sample.

Taxonomy matching defaults to an exact, case-insensitive classification level. Literal substring
matching must be requested explicitly. Results are bounded by pagination and include provenance,
filter facts, and warnings that sequencing read counts are not cell abundance.

`TaraComputeService` provides the current reproducible calculations:

- `taxon_abundance`: raw reads and within-sample relative abundance for one marker and taxon.
- `diversity_analysis`: observed ASV richness and natural-log Shannon index, optionally restricted
  to one taxon and summarized by polar status, ocean region, depth, or size fraction.
- `environment_association`: pairwise-complete Spearman correlation between taxon relative
  abundance and one allowlisted numeric environment variable.

V4 and V9 are never combined. Diversity is not rarefied, grouping is descriptive only, and a
single exploratory Spearman test does not apply multiple-testing correction. These limitations and
undefined calculations are returned as machine-readable warnings.

## Run and test

```powershell
uv run fastapi dev
uv run tara-mcp
uv run pytest
uv run ruff check .
```

`tara-mcp` serves the six read-only MVP tools over stdio. Each tool reuses the analysis-layer
Pydantic request and response contracts, so clients receive generated input and output schemas
plus structured results. Set `TARA_PROCESSED_DATA_DIR` when using a non-default processed-data
directory.

## Trace data contract

Each user request creates one Trace and one nested observation tree. Observation kinds and statuses are defined in `tara_agent.observability.contracts`; callers write nodes through `TraceRecorder` instead of persisting raw span dictionaries directly. Trace contracts, in-process execution context, and persistence lifecycle code live together in the `tara_agent.observability` package.

The recorder redacts common credentials and limits the depth, item count, and text size of node inputs and outputs before persistence. Truncated data includes an explicit marker. Full product responses remain in the Trace output so chat restoration does not depend on bounded developer diagnostics. `context_length` is reserved for the model context-window capacity and remains null when that value is unknown.

LangGraph `tasks` stream events drive workflow-node spans. Node code uses the official runtime `task_id` as the exact parent for nested model, MCP tool, domain service, and processed-data access observations. The persistent runner does not contain a fixed list of node names, so adding a branch, loop, or node changes the recorded execution order without adding matching lifecycle code to the runner. Internal task and observation events are filtered out of the public chat stream.

## Agent and chat API

Set `DEEPSEEK_API_KEY` in `backend/.env`; the default model is `deepseek-flash` and the default
answer reasoning effort is `low`. A request first enters a structured LangGraph routing node.
The router returns `direct_answer`, `analysis`, `clarify`, or `unsupported`. Only `analysis`
continues through the current plan-one-tool, execute, and answer path; the other routes do not
fabricate a tool call. The model never receives source file access, Python, or SQL execution
capabilities.

For an existing session, the router receives at most the latest eight completed user or assistant
messages and 6,000 message characters. Completed analyses are ranked using the current question,
Trace IDs linked by recent messages, recent discussion topics, and recency. A character budget then
determines how many compact references are offered to the router; there is no fixed three-reference
behavior. Each reference keeps the prior question, selected tool and arguments, a bounded result
summary, sources, warnings, and Trace ID; detail rows are not replayed. Planning receives only the
references the router actually uses. Independent questions select no analysis references, and
clarification answers use recent messages without creating an artificial relationship between
Traces. Each Trace remains an independent request record; selected analysis IDs are recorded only
as execution inputs.

Prompts, the capability profile, the top-level workflow, and current tool contracts have stable
resource IDs, explicit versions, and content checksums. The root Trace records resources shared by
the workflow. Routing, planning, and tool nodes record the prompt, capability profile, and tool
contracts they actually use.

Browser clients authenticate with an opaque server-side session. Passwords use Argon2 hashes;
only a SHA-256 hash of each random session token is stored in PostgreSQL. The browser receives an
HttpOnly, SameSite cookie, with Secure enforced in production. Session and trace queries are
always scoped to the authenticated owner.

- `POST /api/v1/auth/register`: create an account and login session.
- `POST /api/v1/auth/login`: create a login session.
- `POST /api/v1/auth/guest`: enter the shared development guest space; disabled in production.
- `POST /api/v1/auth/logout`: revoke the current login session.
- `GET /api/v1/auth/me`: return the authenticated user.
- `GET /api/v1/question-suggestions`: return homepage questions supported by the current processed
  data and registered MCP tools.
- `POST /api/v1/chat`: complete structured response.
- `POST /api/v1/chat/stream`: SSE workflow steps, reasoning deltas, and answer deltas followed by
  the complete structured response.
- `GET /api/v1/sessions`: paginated conversation list.
- `GET /api/v1/sessions/{session_id}`: one conversation and its ordered messages.
- `GET /api/v1/traces`: paginated traces, optionally filtered by `session_id`.
- `GET /api/v1/traces/{trace_id}`: one trace and its ordered workflow spans.
- `GET /api/v1/health`: dataset, Agent, and model readiness.
