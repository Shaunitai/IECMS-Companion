# Adding and verifying approved legal documents

This is the operating procedure for putting a real source into the knowledge
base. Until it is followed, the source cannot ground an authoritative answer.

> **Never** commit confidential court documents, real case records, personal
> data or API keys.

## 1. Confirm you are allowed to use the source

- The document must fall within the source priority order in
  [`SOURCE_POLICY.md`](SOURCE_POLICY.md).
- Website terms, `robots.txt`, access restrictions and reasonable request rates
  must be respected.
- Authentication, CAPTCHAs, paywalls and access controls must never be bypassed.
- If a page cannot be retrieved, record it as inaccessible. Do not recreate its
  contents from memory.

## 2. Add the document to a manifest

Create or edit a manifest JSON file (the bundled
`legal_sources/manifest.json` is the synthetic example — do **not** add real
sources to it). For a real corpus, create e.g.
`legal_sources/manifests/real-sources.json`:

```json
{
  "version": 1,
  "synthetic": false,
  "note": "Approved sources. Each entry verified by a named person before use.",
  "sources": [
    {
      "source_id": "zw-constitution-2013",
      "path": "../documents/zw-constitution-2013.pdf",
      "metadata": {
        "source_id": "zw-constitution-2013",
        "title": "Constitution of Zimbabwe (2013)",
        "issuing_authority": "Parliament of Zimbabwe",
        "document_type": "Constitution",
        "tier": 1,
        "court_or_jurisdiction": "Zimbabwe",
        "publication_date": "2013-05-22",
        "effective_date": "2013-05-22",
        "version_info": "As amended",
        "verification_status": "pending",
        "notes": "Downloaded from the official gazette copy held by ..."
      }
    }
  ]
}
```

`path` is relative to the manifest file. `url` may be used instead, but only for
hosts on `SOURCE_ALLOWED_HOSTS`.

> **Already created.** `legal_sources/manifests/real-sources.json` is the
> definitions corpus. It lists the local `documents/Source Documents/` material
> (tier 1–3) and the English glossary folder (tier 4).

Tier values follow the source policy:

| Tier | Meaning |
|---|---|
| 1 | Legislation and statutory instruments |
| 2 | Court rules, practice directions, circulars, notices |
| 3 | Official JSC publications and procedural guidance |
| 4 | Other authoritative sources, explicitly approved |
| 99 | Synthetic test fixtures (never authoritative) |

## 3. Ingest

```powershell
.\.venv\Scripts\python.exe manage.py ingest_sources --manifest legal_sources\manifests\real-sources.json
```

The command extracts text, records metadata and rebuilds the search index. It
reports, per source, what actually happened. A failed fetch is recorded as
`inaccessible` with the real reason and produces **no** placeholder content.

Check the reported extraction warnings. Common ones:

| Warning | Meaning | Action |
|---|---|---|
| `No text layer found; the PDF may be a scanned image` | The PDF needs OCR | Obtain a text-based copy or run OCR before ingesting |
| `N page(s) produced no text` | Some pages are images | Check the source document |
| `Unsupported content type` | The URL is not a supported format | Save the document locally and ingest it as a file |

## 4. Verify by hand

Verification is a deliberate human act. Nothing sets it automatically, because
a document is never treated as current merely because it is still online.

1. Read the extracted text and confirm it matches the document.
2. Confirm the issuing authority, publication date, effective date and version.
3. Confirm it is still in force (check for a later amendment or repeal).
4. Confirm the tier and jurisdiction are right.

Then record the verification:

```powershell
.\.venv\Scripts\python.exe manage.py verify_source zw-constitution-2013 --date 2026-10-12
```

To retire a source instead:

```powershell
.\.venv\Scripts\python.exe manage.py verify_source zw-constitution-2013 --status superseded --note "Replaced by the 2027 amendment"
```

## 5. Check the result

```powershell
.\.venv\Scripts\python.exe manage.py build_index
```

Then start the server and ask a question the source should answer. Confirm:

- the answer cites the source;
- the source panel shows the correct title, authority and page;
- the verification status reads as expected.

## Verification states

| State | Usable for authoritative answers? |
|---|---|
| `verified` | Yes |
| `pending` | No |
| `outdated` | No |
| `superseded` | No |
| `inaccessible` | No |
| `unverified` | No |
| `synthetic` | Only when `ALLOW_SYNTHETIC_SOURCES=true`, and always disclosed |

## Synthetic fixtures

`legal_sources/manifest.json` and `legal_sources/synthetic/` contain **synthetic
test fixtures**. They exist to exercise the pipeline and are labelled as
synthetic everywhere they surface. Load them only in development:

```powershell
.\.venv\Scripts\python.exe manage.py load_synthetic_corpus
```

They are refused when `ALLOW_SYNTHETIC_SOURCES=False`.

## IECMS procedural manuals

IECMS how-to answers use a **separate** collection so that manual prose cannot
ground a legal definition and a statute cannot be mistaken for IECMS
instructions.

1. Add the manual to `legal_sources/manifests/iecms-manuals.json` (not to the
   synthetic manifest, and preferably not to `real-sources.json`).
2. Set `"ocr": true` in the entry's metadata if the PDF is a scan with no text
   layer. OCR requires the optional `pymupdf`, `winocr` and `pillow` packages
   and, on Windows, an installed English OCR language pack.
3. Ingest:

   ```powershell
   .\.venv\Scripts\python.exe manage.py ingest_sources --manifest legal_sources\manifests\iecms-manuals.json
   ```

4. Verify it like any other source. Re-ingesting resets the status to the
   manifest value (`pending`), so verify again afterwards:

   ```powershell
   .\.venv\Scripts\python.exe manage.py verify_source <manual_source_id> --date 2026-10-18
   ```

5. Add the source id to `IECMS_MANUAL_SOURCE_IDS` (comma-separated). The manual
   collection is enabled by default and can be switched off with
   `ENABLE_IECMS_MANUAL_ASSISTANT=false`.

OCR pages preserve the **PDF page number** in the index, so citations show the
page even when the printed page number differs. OCR text is not perfect; the
assistant is instructed to say when a label or step is garbled rather than guess
it. When the manual does not cover an IECMS question, the open-web fallback may
be used, but its result is labelled unverified and is never presented as
documented manual steps.

## Scanned documents (OCR)

Some approved PDFs are scans with no text layer. OCR them only when the format
makes it worthwhile (dense, printed pages) and always review the result.

1. Set `"ocr": true` in the entry's metadata. `scanned-sources.json` lists the
   known scans separately so OCR can be re-run without re-reading every other
   document.
2. Ingest just the scanned source (OCR is much slower than text extraction):

   ```powershell
   .\.venv\Scripts\python.exe manage.py ingest_sources --manifest legal_sources\manifests\scanned-sources.json --source zw-constitution-amendment-no-20-2013
   ```

   `--source` is repeatable, so several scans can be processed in one run.
3. OCR text keeps the PDF page number and is grouped into paragraphs. Review a
   few passages before relying on it; the ingest warnings record OCR use.
4. Verify the source as usual (see above). Re-ingesting resets the status to
   `pending`.

