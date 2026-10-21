# Privacy

## Principles

IECMS Companion works without accounts and collects as little as possible. It
does not request or require:

- names;
- national identification numbers;
- phone numbers;
- email addresses;
- home addresses;
- case numbers;
- details identifying litigants or other individuals;
- personal case histories.

Document summaries are available, but users are asked not to upload documents
containing personal or confidential case information. An uploaded file is
written to a temporary path only so it can be parsed, and is deleted before the
response is returned. It is never added to the knowledge base, the database or
the logs.

## Safeguards and their status

| Safeguard | Status |
|---|---|
| No user accounts | Implemented |
| No persistent conversation history | Implemented — the visible conversation lives in page memory only (see below) |
| No chat-content analytics | Implemented |
| No database persistence of messages | Implemented — `chatbot` and `legal_sources` define no models |
| Short, configurable message-length limit | Implemented (`CHAT_MAX_QUESTION_LENGTH`, default 500) |
| Visible warning not to submit personal information | Implemented |
| No third-party scripts or analytics | Implemented, asserted by tests |
| No logging of request bodies, sources or answers | Implemented, asserted by tests |
| Uploaded documents are deleted immediately | Implemented — parsed from a temporary file, deleted in `finally`; never stored |
| Basic detection of obvious personal identifiers | Implemented (heuristic) |
| Personal data in follow-up context | Implemented — history entries containing identifiers are dropped before retrieval |
| Rejected input never transmitted to the provider | Implemented, asserted with mocks |

## What the detector actually catches

`chatbot/services/privacy_service.py` matches these patterns:

| Category | Example |
|---|---|
| Email address | `person@example.com` |
| Zimbabwe-format phone number | `0771234567`, `+263 771234567` |
| National-ID-like string | `63-1234567-A-42` |
| Long unbroken digit run | `1234567890` |
| Court case reference | `HC 1234/24` |
| Physical-address-like text | `stand 1234`, `plot 56` |

When a pattern matches, the question is **rejected before retrieval and before
any provider call**, and the rejection message does not echo the identifier.

## Limitations you must disclose

- **This is heuristic and incomplete. It is not anonymisation.** It cannot
  guarantee that all personal information is found. Names, informal addresses,
  oblique references and case narratives cannot be detected reliably by pattern
  matching. This is exactly why the interface also warns users not to submit
  personal information.
- External LLM providers and ordinary network infrastructure may process
  technical metadata (IP address, timestamps, request identifiers) even when
  this application stores nothing.
- Provider privacy terms, free-tier availability and quotas are provider
  policies that can change. No promise of free unlimited service, and no promise
  of confidentiality beyond what a provider guarantees, is made here.
- In generated mode the question and the retrieved source passages are
  transmitted to the provider. In extractive mode (no API key) nothing is sent
  to any provider.

## Development and testing

Only synthetic questions and synthetic fixtures are used in development, tests
and this repository. No real IECMS case records or confidential court documents
are present.

## Retention

| Data | Retention |
|---|---|
| Question text | Not stored on the server. Held in memory for the duration of one request only. |
| Answer text | Not stored on the server. Returned to the browser and discarded there. |
| Conversation history | Not stored anywhere. The visible conversation exists only in the page's JavaScript memory and is lost when the page is refreshed or closed; it is never written to `sessionStorage`, `localStorage` or a cookie. For follow-ups, the browser sends only the last few **previous questions** (no answers, bounded to 6 entries) with the current request. The server uses them in memory for that request only and never stores them. |
| Server logs | Provider failures log the exception class and status code only, never content |
| Source documents | Stored on disk under `legal_sources/documents/` (git-ignored) |
| Source metadata | Stored in `legal_sources/registry.json` (committed, no personal data) |

Django's session middleware is installed but no session data is written by the
chat feature; a test asserts the session stays empty after a chat request.

### Conversation in the page

The question and answer you see stay in the page for as long as it is open. A
refresh starts a new conversation, and no copy is written to browser storage.
Follow-up context is limited to the previous questions described above and is
not retained by the server after the request completes.
