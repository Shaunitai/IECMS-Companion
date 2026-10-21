# Source policy

This policy governs which documents may support an authoritative legal
explanation, how they are obtained and how their currency is tracked.

> **Current state:** a real *definitions corpus* has been added in
> `legal_sources/manifests/real-sources.json`. It lists official local copies
> (legislation, court rules, a practice direction, a court form and the IECMS
> user manual) plus one approved public language glossary. Each entry still has
> to be verified by an authorised person before it may ground an answer. Three
> entries are scanned images with no text layer and cannot ground an answer
> until they are OCR'd. The synthetic fixtures remain, for tests only — see
> [`ADDING_SOURCES.md`](ADDING_SOURCES.md).

## Source priority

An authoritative explanation may rely on sources in this order, mirrored by the
`tier` field:

| Tier | Source type |
|---|---|
| 1 | Applicable official Zimbabwean legislation and statutory instruments |
| 2 | Official court rules, practice directions, circulars and notices |
| 3 | Official JSC publications and procedural guidance |
| 4 | Other authoritative legal sources, **only** when explicitly approved and clearly distinguished from primary official sources |
| 99 | Synthetic test fixtures — never authoritative, always disclosed |

## Obtaining sources

- Only authorised local files and approved public URLs may be ingested.
- Website terms, `robots.txt`, access restrictions and reasonable request rates
  are respected. `robots.txt` is read before each fetch; if it cannot be read,
  the fetch is **refused**.
- Authentication, CAPTCHAs, paywalls and access controls are never bypassed.
- If a page cannot be accessed it is recorded as `inaccessible` with the real
  reason. Its contents are never invented.

### Definitions corpus

Terminology and definition questions are answered from two kinds of source,
held in the local corpus or fetched from an approved host:

| Location | Contents | Tier |
|---|---|---|
| `legal_sources/documents/Source Documents/ACTS OF PARLIAMENT/` | Acts and a statutory instrument | 1 |
| `legal_sources/documents/Source Documents/COURT RULES/` | High Court, Supreme Court and Magistrates Court rules | 2 |
| `legal_sources/documents/Source Documents/PRACTICE DIRECTIONS AND OFFICIAL COURT NOTICES/` | Practice directions and notices | 2 |
| `legal_sources/documents/Source Documents/COURT FORMS AND FILING TEMPLATES/` | Court forms and filing templates | 2 |
| `legal_sources/documents/Source Documents/IECMs user manual, help guides, and approved interface/` | IECMS interface guidance | 3 |
| `legal_sources/documents/Source Documents/LEGAL ENGLISH GLOSSARY/` | English word-usage reference | 4 |

Tier 4 entries are language references, not legal authorities. They may help
explain *what a word means*; they are never cited as law.

The folder is git-ignored (`legal_sources/documents/`), so the originals are not
committed; only the manifest and the registry are.

### Recorded finding

`www.jsc.org.zw` and `jsc.org.zw` were **not reachable** from the development
environment (all requests timed out). This is recorded here rather than worked
around. No JSC content was reproduced, summarised or guessed.

Because of that, the definition corpus relies on local official copies rather
than on fetching from the JSC portal. The JSC hosts remain on the allow-list
for when they become available.

### Scanned documents

Three corpus entries are scans without a text layer:

- the Constitution of Zimbabwe Amendment (No. 20) Act, 2013;
- the IECMS user manual;
- the Oxford Collocations Dictionary for English.

Extraction records them with a `No text layer found; the PDF may be a scanned
image and would need OCR` warning and **no** passages, so they are searchable as
metadata but cannot ground an answer. They must be OCR'd, or replaced with a
text-based copy, before they can be used. Their contents are never guessed.

## Required metadata

Every document carries:

| Field | Notes |
|---|---|
| `title` | |
| `issuing_authority` | e.g. Parliament, a court, the JSC |
| `source_url` / `local_path` | provenance |
| `document_type` | Act, statutory instrument, rule, practice direction, notice |
| `court_or_jurisdiction` | |
| `tier` | see table above |
| `publication_date`, `effective_date`, `end_date` | where known |
| `last_verified` | the date a human last confirmed it |
| `version_info` | amendment / version description |
| `verification_status` | see states below |
| page numbers and section headings | preserved during extraction |
| `extraction_warnings` | e.g. probable scanned PDF, unreadable pages |

## Verification states

| State | Meaning | May ground an authoritative answer? |
|---|---|---|
| `verified` | Confirmed official and current as of `last_verified` | **Yes** |
| `pending` | Retrieved but not yet verified | No |
| `outdated` | Superseded by a later version | No |
| `superseded` | Explicitly replaced | No |
| `inaccessible` | Could not be retrieved | No |
| `unverified` | Provenance or currency unknown | No |
| `synthetic` | Test fixture | Only when `ALLOW_SYNTHETIC_SOURCES=true`, and always disclosed |

Two rules are enforced in code and covered by tests:

1. **A document is never treated as current merely because it is still online.**
   Only an explicit `verified` status qualifies.
2. **Verification is a deliberate human act.** Nothing in the pipeline sets it.
   The `verify_source` command is the only way to change it.

Where currency or applicability is unknown, the uncertainty is disclosed rather
than hidden.

## Open-web fallback (on by default)

When `ENABLE_WEB_FALLBACK=true` (the default) and the approved corpus cannot
establish an answer, the assistant searches the open web instead of refusing.

This **deliberately relaxes** the "no approved evidence, no answer" rule, so it
is enabled knowingly and can be switched off (`ENABLE_WEB_FALLBACK=false`) to
restore the strict rule. When it is used:

- the response status is `web_fallback` and the label reads
  `WEB SOURCE (UNVERIFIED)`;
- every cited page carries its exact URL, its host and the retrieval date, and
  is marked `web_unverified` — never `verified`;
- the answer states that it rests on unverified web pages, not on the approved
  collection;
- a fetched page is treated as untrusted data, exactly like an ingested
  document;
- only sentences that mention a meaningful term from the question are used,
  and off-topic pages are dropped;
- IECMS questions prefer the approved manual; the fallback is used only when
  the manual does not cover the question, and its result is never presented as
  documented manual steps;
- `robots.txt` is honoured before the search and before every fetch, and
  requests are throttled.

Search uses the DuckDuckGo HTML endpoint (`html.duckduckgo.com`), whose
`robots.txt` permits it. Recorded honestly: DuckDuckGo's instant-answer API host
(`api.duckduckgo.com`) **disallows** robots access, and scraping any search
engine is a terms-of-service grey area. A keyed provider (Brave, Tavily,
Google) can replace
`chatbot/services/web_search_service.py::_search_duckduckgo` without changing
anything else.

## Prohibited content

- Do not fabricate documents, rules, citations, URLs or verification metadata.
- Synthetic material may be used **for development and tests only**, must be
  labelled as synthetic in its metadata *and* its text, and is refused when
  `ALLOW_SYNTHETIC_SOURCES=false`.
- Do not commit confidential court documents, real case records or personal
  data.
- Downloaded originals, extracted text and the index stay out of version
  control (see `.gitignore`).

## Untrusted input

Retrieved documents are treated as **data, not instructions**. Any instruction
embedded in a document that attempts to change the system's role, override these
rules or reveal secrets is ignored. This is stated in the model's system
instructions and tested with a deliberately hostile fixture.
