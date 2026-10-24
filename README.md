# Ghost Data Still Exists: deletion-audit agent

For every request in `dataset/deletion_requests.csv`, this agent:

1. decides whether the person actually asked for deletion;
2. resolves their current and historical identifiers;
3. finds every record and data flow the evidence ties to them;
4. reconstructs each record's state at `audit_at`;
5. applies `deletion_rules.csv`.

It writes `output.csv` with `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` for each request. Each row
carries the ordered findings, remediation and verification steps, and each step cites the evidence
that proves it.

The design rule is **"the model describes, deterministic code decides."** Code does the parsing,
identity resolution, linking, state reconstruction, policy, ordering and citations. A language model
(DeepSeek `deepseek-flash` in the submitted run) handles only the judgments that parsing cannot make
reliably. Its answers are merged under fixed guardrails. Without an API key the same pipeline runs
fully deterministically.

The challenge contract is in `problem_statement.md` and `AGENTS.md` in the challenge repository.

## Results at a glance

| Set | Result |
|---|---|
| 32 public samples (scored against `sample_output.csv`) | status accuracy 1.000, step precision / recall / F1 1.000 / 0.896 / 0.945, sequence accuracy 0.562 (18/32), 0 contract violations |
| 4 hand-built cases (`code/evaluation/custom_cases.py`) | status accuracy 0.750 code-only, 1.000 with the model review |
| Final full run (518 requests) | 154 COMPLETE, 354 INCOMPLETE, 10 NOT_APPLICABLE, 2,855 steps, 0 contract violations, all 1,073 citations exist and predate `audit_at`; 36 min, 530 billed model calls, $0.315 |

Details: [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md) and
[`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

## Setup

You need:

- **Python 3.12.** The code was developed and tested on 3.12.3. Run everything inside the virtual
  environment: the system interpreter lacks the dependencies (`pydantic` is imported at start-up).
- **Optional: Tesseract OCR** (`tesseract` on `PATH`, tested with 5.3.4). It is the
  deterministic reader for the 11 screenshots and the backstop for the model's transcripts.
  Without it, and without an API key, screenshots contribute nothing.

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r code/requirements.txt   # anthropic, openai, pydantic, pypdf, pytest (pinned)
sudo apt install tesseract-ocr         # optional; macOS: brew install tesseract
```

To turn on the model review, create `.env` in the repository root. It is git-ignored; our
repository's `.env.example` is the template.

```text
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=<your key>
DEEPSEEK_MODEL=deepseek-flash
```

## Configuration

`code/deletion_audit/llm_client.py` reads these from `.env` at the repository root or from the
environment. A real environment variable wins over `.env`. Keep comments in `.env` on their own
lines, because values are read verbatim to the end of the line.

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | auto | `deepseek` or `anthropic`. If empty, whichever key is set picks the provider. |
| `DEEPSEEK_API_KEY` | (none) | DeepSeek key. With no usable key the run is deterministic only. |
| `DEEPSEEK_MODEL` | `deepseek-flash` | DeepSeek model name. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `ANTHROPIC_API_KEY` | (none) | Anthropic key, for the alternative backend. |
| `AUDIT_MODEL` | `claude-opus-5-5` | Anthropic model name. |
| `AUDIT_EFFORT` | `medium` | Anthropic effort level. |

Secrets are read only from the environment. `.env` is git-ignored and is not part of `code.zip`.

## Running

Run these with the virtual environment active. Default paths resolve from the repository root, so
the commands work from any directory. Relative paths you pass in flags resolve from the current
directory.

```bash
python3 code/main.py                                  # full run: 518 requests -> output.csv
python3 code/main.py --no-llm                         # deterministic pipeline only, no model calls
python3 code/main.py --requests dataset/sample_deletion_requests.csv --output /tmp/sample.csv
python3 code/evaluation/main.py                       # score samples + custom cases
python3 code/evaluation/main.py --set match_workflow_outcome=false   # one-flag ablation
python3 code/evaluation/usage_summary.py --write      # usage_report.md from the latest full run
python3 -m pytest -q code/tests                       # 17 unit tests, offline
```

A full run with the model makes one review call per case, one at a time. It logs progress every 50
cases and takes about 36 minutes and $0.31 at DeepSeek off-peak prices. Answers replayed from the
local cache cost nothing and finish in seconds. `--no-llm` needs no network. Once Tesseract's
text for the 11 screenshots is cached it runs in under a second; the first run also OCRs them.
`main.py` validates the file it wrote and exits 1 on any contract violation.

## Inputs and outputs

Read from `dataset/`:
- the requests;
- `user_identities.csv` and `user_audit_log.csv`;
- `system_activity.csv`;
- `deletion_rules.csv`;
- `evidence_files.csv` and every file it lists: 24 SQL, 26 log, 28 JSON, 1 TXT, 4 PDF and 11 PNG.

Written:

| Path | Contents |
|---|---|
| `output.csv` | `case_id,deletion_status,steps_json`, one row per request, sorted by `case_id` in natural order (case_33 ... case_550, the same order as the request file). Each step has exactly the 7 contract fields and is serialized as compact JSON. |
| `.cache/llm/*.json` | Validated model answers, keyed by a hash of provider, model, prompt (version and text), schema and input. |
| `.cache/llm/usage.jsonl` | One line per billed API call: tokens, cost, latency, timestamp and cache key. |
| `.cache/llm/runs/*.json` | One record per model-enabled run, listing every answer it used (live, cached or failed). |
| `.cache/ocr/*.txt`, `.cache/eval/*.json` | Tesseract text per screenshot, and saved evaluation runs. |

`.cache/` is git-ignored and is not in `code.zip`.

## Architecture

```text
start-up  load the CSVs; parse all 94 evidence files once (screenshots read by the model if configured)
per case  1. intent    rule classifier on request_text                    intent.py
          2. identity  email -> user_id and every historical identifier   identity.py
          3. link      evidence records tied to those identifiers         linking.py
          4. state     latest observation in [requested_at, audit_at]     linking.py
          5. review    one model call, merged by code (optional)          adjudication.py
          6. policy    deletion_rules.csv -> finding, fix, check          policy.py
          7. steps     order, number, cite; NOT_APPLICABLE -> []          steps.py
end       write and validate output.csv; save the run record              output.py, main.py
```

The deterministic pass always runs first. When a model is configured, it reviews the case. Code
merges the review and re-applies the policy. If the model call fails, the deterministic result
stands.

| Stage | Module | What it does |
|---|---|---|
| Load | `dataset.py` | Reads the CSVs into typed rows. |
| Identity | `identity.py` | Maps the request email to a `user_id` through the current snapshot and the audit-log history. It resolves only when the email has exactly one owner. Subject keys are the `user_id` plus every current and historical email, email hash and phone (digits only). |
| Evidence | `evidence/*.py`, `screenshot_reader.py` | Turns every file into `Observation` records (resource, system, category, exists, direct identifiers, user references, link keys, enabled / restore flags). Snapshots also produce `Coverage` records, which prove absence. Parsers: SQL (executed in in-memory SQLite, replicas included), JSON (bundles, exports, collection dumps, ledgers), logs (seven layouts grouped into traces), the TXT handover, PDFs (text layer via `pypdf`), screenshots (model transcript plus Tesseract backstop). Each file is parsed in isolation, so one bad file cannot stop the run. |
| Activity | `activity.py` | Extracts the few activity rows that carry facts: data-catalog `LINK_RESOURCE_IDENTITY` rows, point-in-time `CHECK_*` rows, workflow batches (requests that share a `reported_at`), and the operation the workflow requested for each resource (context for the model). |
| Linking + state | `linking.py` | Links records to the subject's keys using evidence captured by `audit_at`, and takes each record's state from observations inside [`requested_at`, `audit_at`]. Also recovers accounts from workflow batches (below). |
| Intent | `intent.py` | Rule-based classifier: the fallback when no model is configured, and the hard stop for explicit "not asking for deletion" wording. |
| Model review | `adjudication.py`, `prompts/adjudication.md` | One structured call per case, merged under guardrails (next section). |
| Policy | `policy.py` | The only place that maps (category, state) to operations, per `deletion_rules.csv`. |
| Steps + output | `steps.py`, `output.py` | Builds, orders, numbers and serializes steps; writes and validates `output.csv`. |
| Orchestration | `pipeline.py`, `main.py` | Runs cases in order and isolates each one: an exception is logged and the case gets a fallback `INCOMPLETE`, `[]` row. Saves the run record. |
| Model client | `llm_client.py` | DeepSeek and Anthropic behind one `complete_json` call: response cache, one corrective retry, usage log, run record. |
| Policy flags | `config.py` | `AuditPolicy` holds the interpretation choices the samples do not settle, each with an ablation in the evaluation report. |

### Where the model is used, and how it is contained

The model has two jobs:

1. **Case review** (`adjudication.py`): one call per case. The model receives a dossier: case
   times, the subject's identifiers, and each candidate record with its link basis, linking
   excerpts and state at audit. The request text is passed verbatim, and both are marked as
   untrusted data. It returns the request's intent (with type and language), whether it saw an
   injection attempt, and for each record whether it belongs to the subject (yes / no /
   uncertain) and its category.
2. **Screenshot transcription** (`screenshot_reader.py`, `prompts/screenshot.md`): one vision call
   per screenshot, made once per run and shared by all cases. It transcribes each record shown:
   ID, system, identifiers, names, and exists / enabled / restore flags.

Code applies the review under rules that a manipulated answer cannot get around
(`CaseAdjudicator.merge`):

- **Intent:** the model's reading is used, except that explicit non-deletion wording in the
  request always wins. If the model reports an injection while the plain text reads as deletion,
  the deletion reading is kept.
- **Injection:** once the model reports an injection, none of its record or category judgments
  are applied.
- **Dropping a record:** a "no" verdict removes a record only if it was softly linked (catalog
  claim, workflow batch, related record, screen or document only) and the record's own
  identifiers name someone else. Otherwise the record stays and a note is logged.
- **Categories:** the model can change a category only when the parser had nothing better than
  the system default, and only to a category allowed for that system. For example, a support
  record can become an attachment.
- **Limits:** the model cannot add records or change parsed states.
- **Validation:** answers are validated with pydantic models; the JSON schema is part of the
  request. An invalid reply is retried once with the error attached. If the call still fails, the
  deterministic result stands.

## Design decisions

- **Claims are not proof.** Reported statuses and `reported_result` values never establish state.
  Activity rows serve four purposes only: as catalog links, as point-in-time checks when no
  evidence file covers the record, for pairing workflow batches with requests, and to show the
  model the workflow's requested operation as context.
- **Linking and state use different windows.** Identity is stable, so any evidence captured by
  `audit_at` can link a record. State comes only from observations made between `requested_at` and
  `audit_at`, because earlier evidence shows the pre-deletion state. A record seen only before the
  request produces no step. The one exception is an account with an absence proof, kept when it is
  the subject's only account candidate. Turning this rule off drops sample precision to 0.961.
- **Exact identifiers only.** Matching is exact after normalization: lower-cased emails, hashes and
  user IDs, digits-only phones. Names never link on their own, because three names in the data
  belong to two people each and look-alike decoy emails exist.
- **The latest observation wins.** At the same instant, file evidence beats an activity check.
  Within one SQL snapshot, the most exposed copy across primary and replica tables counts, which
  is how replica drift becomes a finding.
- **Absence needs proof.** Apart from batch-recovered accounts (next point), a record is treated as
  absent only on an observation, or on a snapshot that covers its system and lacks it. A record
  whose state the evidence does not establish produces no step at all.
- **Account recovery.** Some accounts appear only in deletion-worker rows inside a workflow batch
  shared by several requests. Recovery removes accounts that evidence ties to someone else, then
  assigns by elimination across all batches, sudoku-style. It also requires the account step's
  reported result to agree with the request's `reported_status`; that is used only for pairing,
  never as proof. Ambiguous batches stay unassigned. Such an account counts as absent: in an
  `INCOMPLETE` case it gets a future `VERIFY_RECORD_ABSENT`, and in a `COMPLETE` case it is
  verified only when an observation or snapshot proves the absence.
- **Screenshots link only through the data catalog.** A screen is a list view. Only the record a
  catalog row anchors, by ID or display alias, can link to a person, and only if the screen shows
  that person or shows nobody. Email misreads are tolerated with a 0.85 similarity threshold.
  Other rows contribute state but never create links.
- **Policy.** Each rule maps a category to one treatment:
  - DELETE: still present → remaining record → delete;
  - REDACT: direct identifiers present → exposed PII → redact (a stable `user_id` may remain, as
    in tombstones);
  - ANONYMIZE: any route back to the person, including a `user_id` → anonymize;
  - RETAIN: verify the record is present;
  - enabled data flow → disable;
  - backup with restore allowed → block the restore (the backup itself is retained).

  `COMPLETE` means zero findings.
- **Steps and citations.**
  - `COMPLETE` rows contain only verification steps, each citing the evidence of the current state.
  - `INCOMPLETE` rows contain findings that cite evidence, one remediation per finding, and
    verification for every finding, every account and every retained record. They also verify
    records that are already compliant (`verify_compliant_records_when_incomplete`).
  - Remediation and future verification steps cite `""`.
  - Steps are ordered by phase, then operation order in the problem statement's tables, then
    `system_id`, then `resource_id`.
- **Determinism.** Sampling settings do not make a model reproducible, so every validated answer
  is cached by input hash and a re-run replays it exactly. Output is sorted and serialized
  deterministically.
- **Provider.** DeepSeek `deepseek-flash` was chosen for cost: $0.0006 per request off-peak, with
  JSON-mode output and vision. An Anthropic backend is implemented behind the same interface
  (structured outputs, prompt caching) but was not exercised in our runs.

## Evaluation and usage reports

- **[`evaluation_report.md`](code/evaluation/evaluation_report.md)** covers:
  - metric definitions and results on the samples;
  - the remaining misses by type;
  - the model's before/after effect;
  - the final full-run validation and diff against the code-only output;
  - the iteration log, one-flag ablations, the four custom cases, and failures found and fixed.

  Its metrics come from `code/evaluation/main.py` (scoring in `scoring.py`), which saves each run
  under `.cache/eval/`.
- **[`usage_report.md`](code/evaluation/usage_report.md)** is generated from the run record of the
  run that produced `output.csv`. It shows calls, tokens (cache-miss input, provider cache hits,
  output), cost per purpose, per model and overall, per-request averages, and pricing. Answers
  replayed from the cache are counted at the cost of the call that originally produced them, so
  the totals cover everything behind `output.csv`.
- **Tests** (`code/tests/`, 17, offline) cover:
  - policy decisions and step ordering;
  - intent rules;
  - six log layouts;
  - the merge guardrails, including injection;
  - screenshot anchoring;
  - client retries, caching, provider selection and pricing;
  - the usage-report join.

## Known limitations

- **Recall has a ceiling.** Before any heuristics, 34 of the 152 expected finding/verification
  steps in the samples (22%) had no identifier link in evidence available by `audit_at`. Some
  analytics records are anonymized with no identifier left. Some audit records appear only after
  `audit_at`, or in no participant file. All 20 remaining sample misses are missing steps; there
  are no wrong or extra steps.
- **Accounts.** 172 of the 508 eval deletion cases have no account step: no evidence names the
  account, and batch recovery could not assign one unambiguously. Another 170 accounts come from
  workflow-batch recovery, which infers the pairing from workflow claims rather than proving it
  from evidence.
- **case_63 is `COMPLETE` with no steps.** Its only candidate record (`marketing_001`) is claimed
  for the subject by the data catalog, but the screenshot shows a different person, so the claim
  is rejected and nothing is left to verify.
- **The model changed little on this data.** It changed 1 of 518 eval outputs (case_34, a support
  attachment visible only in a screenshot) and 0 of 32 samples. It agreed with the rule classifier
  on intent in all 518 cases. Its measured value is on non-English negations and injected
  instructions (custom cases) and on reading screenshots.
- **Reproducing the exact output needs the local response cache, which `code.zip` does not
  include.** A fresh run asks the model again (about $0.31 and 36 minutes, calls one at a time),
  and its answers can differ where the model reads a screenshot or a request differently.
- **Interpretation choices the samples do not settle** are encoded as `AuditPolicy` flags or plain
  rules:
  - already-compliant records are verified in `INCOMPLETE` cases;
  - `resource_id` ties sort as strings;
  - a file is usable only when its manifest `captured_at` is at or before `audit_at`, even if
    its inner timestamps are earlier;
  - when several observations prove the same state, the latest is cited.

## Repository layout

```text
code/
├── main.py                     entry point (CLI)
├── requirements.txt
├── deletion_audit/
│   ├── config.py               paths, AuditPolicy flags
│   ├── domain.py               enums and dataclasses (requests, observations, steps)
│   ├── dataset.py              CSV loaders
│   ├── identity.py             email -> subject and all historical identifiers
│   ├── activity.py             catalog links, activity checks, workflow batches
│   ├── evidence/               one parser per format -> Observation / Coverage; store.py indexes them
│   ├── screenshot_reader.py    vision transcription of screenshots
│   ├── linking.py              linking, state at audit, absence proofs, account recovery
│   ├── intent.py               rule-based intent (fallback and hard stop)
│   ├── adjudication.py         per-case model review and guarded merge
│   ├── policy.py               deletion_rules.csv -> finding / remediation / verification
│   ├── steps.py, output.py     ordered steps, CSV writing and validation
│   ├── pipeline.py             per-case orchestration and isolation
│   ├── llm_client.py           providers, response cache, retries, usage log, run record
│   └── prompts/                adjudication.md, screenshot.md
├── evaluation/
│   ├── main.py, scoring.py     sample + custom-case evaluation
│   ├── custom_cases.py         four hand-built cases with expected outputs
│   ├── usage_summary.py        builds usage_report.md
│   ├── evaluation_report.md
│   └── usage_report.md
└── tests/                      17 offline unit tests
```

## Packaging `code.zip`

From the repository root (the archive holds `README.md` and `code/`, never `dataset/`, `.env`,
`.cache/` or the virtual environment):

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

On Windows (PowerShell), remove any `code/__pycache__` folders first:

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

The archive must not contain nested archives. Submit `code.zip`, the root `output.csv`, and
`log.txt` as the chat transcript.
