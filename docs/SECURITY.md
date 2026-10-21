# Security review

A review of the application's security posture, recorded honestly including what
has **not** been verified.

## Threat model

| Asset | Threat | Control |
|---|---|---|
| Provider API key | Leaked to the browser or to Git | Server-side only; read from environment; never rendered into a template |
| User messages | Stored, logged or sent to an unintended party | No persistence; no content logging; local privacy scan before any provider call |
| Legal accuracy | Fabricated citations or invented rules reaching a user | Grounding required; citations validated; no-evidence path refuses |
| Sources | Poisoned documents instructing the model | Sources treated as untrusted data; explicit system rule to ignore embedded instructions |
| Cross-site request forgery | Third-party site triggering chat requests | `@csrf_protect` on the endpoint; `SameSite` session cookie |
| Cross-site scripting | Model or source output injecting markup | Answers inserted with `textContent`; Django autoescaping in templates; JSON responses |
| Denial of service | Huge inputs or unbounded retries | Question length limit; provider call timeout; bounded attempts |

## Controls present

- **Secrets.** `LLM_API_KEY` is read from the environment only. There is no code
  path that writes it to a template, a response body or the logs. Covered by
  tests that assert the page and the chat response never contain the key.
- **CSRF.** The chat endpoint is decorated with `@csrf_protect`; a strict test
  client confirms a request without a token is rejected with 403.
- **Input validation.** JSON shape, object type, question length, and language
  choice are all validated before any work happens. Errors return structured
  codes (`invalid_json`, `invalid_payload`, `invalid_input`).
- **Privacy gate runs first.** A question containing identifiable data is
  rejected before retrieval, so nothing reaches the provider. Asserted with
  mocks that fail the test if retrieval or the provider is called.
- **Grounding gate.** The provider is never called without retrieved evidence,
  and an answer citing nothing valid is not shown as grounded.
- **Citation validation.** Markers are checked against the labels actually
  supplied; unknown markers are stripped from the text.
- **Bounded retries.** Only transient failures are retried, up to
  `LLM_MAX_ATTEMPTS`, with capped backoff.
- **Content-free logging.** Provider failures log the exception class and the
  status code only. Tests assert that neither the question text nor the answer
  text appears in the logs.
- **Safe outbound links.** Source links use `rel="noopener noreferrer nofollow"`
  and `target="_blank"`.
- **No third-party scripts.** Tests assert the templates and JavaScript contain
  no external URLs and no analytics.
- **No chat persistence.** `chatbot` and `legal_sources` define no models; the
  session stays empty.

## Fetching rules

`SourceIngestor` refuses non-HTTP(S) schemes, refuses hosts outside
`SOURCE_ALLOWED_HOSTS`, honours `robots.txt` (treating an unreadable
`robots.txt` as "disallowed"), caps download size, applies a request timeout and
enforces a minimum delay between requests to the same host. It never bypasses
authentication, CAPTCHAs or paywalls.

## Deployment caveats — read before exposing this publicly

The following are **not** production ready and have not been verified:

- `DEBUG` defaults to `true`. A deployment must set `DJANGO_DEBUG=false` and
  provide `DJANGO_SECRET_KEY`; the application raises at startup otherwise.
- Settings have not been hardened for production: `SECURE_HSTS_SECONDS`,
  `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` and
  `SECURE_PROXY_SSL_HEADER` are not configured.
- There is **no rate limiting**. A public deployment needs it — the LLM call is
  a paid, externally rate-limited resource.
- The development server is not a production server. Use a real WSGI/ASGI
  server behind TLS.
- The privacy detector is a heuristic and is not a substitute for a Data
  Protection Impact Assessment.
- No penetration test has been performed.
- Dependency versions are pinned in `requirements.txt` but no automated
  vulnerability scanning (for example `pip-audit`) is wired in.

## Not verified

- No real provider call was made while building this: no API key was available,
  so the LLM path is verified only against mocked providers.
- Browser speech synthesis was not verified on a device with an installed
  English voice.
- The application has not been deployed or exposed to the internet.
