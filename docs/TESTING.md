# Testing and evaluation guide

## Running the tests

```powershell
.\.venv\Scripts\python.exe manage.py test
```

A single module or class:

```powershell
.\.venv\Scripts\python.exe manage.py test chatbot.tests.test_pipeline
.\.venv\Scripts\python.exe manage.py test chatbot.tests.test_pipeline.CitationValidationTests
```

## The suite cannot reach the network

`config/test_runner.NoNetworkTestRunner` is configured as `TEST_RUNNER`. It:

1. replaces the provider SDK loader with a function that raises, so a test that
   forgets to mock `llm_service.generate_explanation` fails loudly instead of
   making a real, billable API call; and
2. blocks `urllib.request.urlopen`, so no test can fetch a remote document.

This exists because an earlier version of the suite did make one real request to
the Gemini API (see `docs/SECURITY.md`). It is a deliberate regression guard.

## Test modules

Last verified run: **184 tests, all passing** (Django 6.1.2, Python 3.14.8,
Windows).

| Module | Tests | Covers |
|---|---|---|
| `test_interface.py` | 32 | Startup, IECMS scope copy, routes, UI requirements, input validation, length limits, CSRF, no-evidence behaviour, mock mode, no persistence, static-asset guards |
| `test_corpus.py` | 41 | Metadata and verification states, extraction (Markdown, HTML, DOCX, PDF, headings), tokenization, index build/search/persistence, ingestion, robots refusal, allow-list, inaccessible handling, manifests |
| `test_pipeline.py` | 32 | Retrieval correctness and authorisation, citation validation, provider configuration, failure classification, extractive answers, mocked provider answers |
| `test_privacy_safety.py` | 22 | Personal-identifier detection and enforcement, advice screening, prompt-injection blocking, system-instruction content, speech-support handling |
| `test_adversarial.py` | 22 | Document-embedded instructions, fabricated citations, advice boundary under the provider path, log hygiene, hostile input, no third-party requests, fixture honesty |
| `test_manual.py` | 20 | Intent routing, manual citations, web fallback when the manual is silent, follow-up context, step extraction, OCR paragraphing and furniture removal |
| `test_web_fallback.py` | 15 | Open-web fallback gating, unverified labelling, citation requirements, query-aligned filtering, robots refusal, provider selection |
| `test_evaluation.py` | 5 | The terminology dataset: expected source, required and forbidden answer characteristics |

## What is deliberately asserted

- A question with no matching approved source returns `insufficient_evidence`
  and **the provider is never called**.
- A synthetic source is excluded when `ALLOW_SYNTHETIC_SOURCES=false`, and a
  `pending` source is never authoritative.
- A fabricated citation marker is removed from the answer text, and an answer
  with no valid citation is not displayed as grounded.
- Personal data is rejected **before** retrieval, asserted with a mock that
  fails the test if retrieval runs.
- An injected instruction inside a document does not change pipeline behaviour.
- Neither the question text, the source text nor the answer text appears in
  logs.
- The API key never appears in the page or in any response body.

## Evaluation dataset

`tests/evaluation/terminology_questions.json` holds fictional, general
terminology questions. Each entry specifies:

- `expected_status` — for example `extractive` or `insufficient_evidence`;
- `expected_source_ids` — the synthetic fixture that should ground the answer;
- `required_characteristics` — for example `cites_expected_source`,
  `redirects_to_practitioner`, `discloses_synthetic`;
- `forbidden_characteristics` — for example `case_specific_advice`,
  `invents_deadline`, `fabricated_source`.

`test_evaluation.py` translates those names into checks and grades the
**evidence and citations**, not whether an answer sounds plausible. Forbidden
characteristics are matched with explicit patterns (`"you should"`,
`"within N days"`, `"as your lawyer"`).

### Adding a case

1. Append an entry to the dataset with a fictional question.
2. Use only synthetic fixture ids from `chatbot/tests/corpus_fixtures.py`.
3. Keep characteristics to the names the checker understands, or extend
   `_required_characteristic` / `_forbidden_characteristic` in
   `test_evaluation.py` at the same time.

## Rules

- Use synthetic source documents and mark them as test fixtures, never as
  genuine legal authorities.
- Do not claim a test passed without running it and reporting the real result.
- Do not let a test reach the network. Mock the provider or the fetch function.

## Not covered by automated tests

- Real provider behaviour (no API key was available; the provider path is
  verified only against mocks).
- Browser speech synthesis with an installed English voice on real devices.
- Retrieval relevance quality — the keyword limitation described in
  `ARCHITECTURE.md` is documented, not solved.
- Real OCR output — the suite tests OCR paragraphing with synthetic
  `OCRPage` objects; the Windows OCR engine is not invoked during tests.
- Load, penetration and accessibility testing with real assistive technology.
