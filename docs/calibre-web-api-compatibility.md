# Calibre-Web NextGen integration

Validated against upstream `new-usemame/Calibre-Web-NextGen` tag
`v4.1.74`.

## Current implementation

The integration is implemented on `feat/calibre-web-integration` and is
available for review in the draft pull request. It has not been deployed.

- One CWNG connection per LibrisLog user. The CWNG URL and username are stored
  as connection metadata; the password and reusable Flask session cookie are
  encrypted with LibrisLog's existing Fernet helper. CSRF tokens are fetched
  when a session is opened and are not persisted.
- A separate durable mapping links a LibrisLog book to an explicitly selected
  CWNG book ID, scoped to the user's CWNG connection. The request currently
  accepts only the `default` library identifier because explicit multi-library
  selection has not been verified. ISBN and title are candidate hints only,
  never permanent identifiers.
- Upload history records the filename, format, processing state, CWNG task ID
  (when found), and CWNG book ID after confirmation. Ebook bytes are not stored
  in the integration tables.
- EPUB and PDF uploads are limited to 100 MB, validated by file signature, and
  forwarded using a sanitized basename. Existing formats can only be replaced
  after the user explicitly confirms replacement.
- CWNG upload tasks are correlated using task IDs observed before upload,
  the uploaded filename, and the authenticated account's CWNG display name
  from `/api/v1/auth/me`. The upload API itself does not return a task ID.
  A task is attached only when exactly one new candidate matches; ambiguous
  candidates are deliberately left unmatched. If a task remains invisible for
  15 minutes, the local upload is marked failed with an explicit message
  rather than polling forever. The user must verify the CWNG library before
  retrying or linking a result.
- Upload completion does not silently create a permanent book mapping. The
  user searches CWNG, selects a candidate, and LibrisLog verifies that the
  selected book is readable by the mapped account before saving the mapping.
- Existing CWNG books can also be linked directly from LibrisLog without
  uploading duplicate files.
- Linked EPUB/PDF files expose CWNG reader and download links in the LibrisLog
  book drawer. The browser uses its own CWNG login session for those links.
  Remote lookup errors are surfaced separately from a successful empty format
  list; the UI no longer silently presents connection failures as no files.
- Metadata sync is one-way from LibrisLog to CWNG: title, authors (when
  present), publisher, publication date, description, ISBN, and page count
  when CWNG's book detail exposes a populated integer custom column named
  `pages` or `page_count`. Unknown/empty LibrisLog values are omitted from
  updates, so they do not erase richer CWNG metadata. CWNG currently omits
  empty custom columns from that detail response, so an empty page-count
  column cannot yet be discovered or initialized by this integration. Existing
  non-ISBN identifiers are preserved.
- Language and cover projection are not implemented. Reading status, dates,
  ratings, personal notes, acquisition state, and reading progress are never
  sent to CWNG.
- Metadata is re-projected after book edits saved through the LibrisLog book
  drawer; a manual sync action is also available.
- Disconnecting or deleting local LibrisLog records removes only local
  credentials, mapping, and upload-history records. It never deletes remote
  CWNG books or ebook files.

## Confirmed CWNG API behaviour

The following behaviour was checked against the pinned upstream tag:

- `GET /api/v1/auth/csrf` returns a CSRF token.
- `POST /api/v1/auth/login` accepts a normal CWNG username/password and
  establishes a Flask-Login session.
- `GET /api/v1/auth/me` returns account permissions, feature flags, and the
  display name used by CWNG's task list.
- `POST /api/v1/upload` queues new files for asynchronous ingest and returns
  queued filenames and per-file errors, not a Calibre book ID or task ID.
- `POST /api/v1/books/{book_id}/formats` queues a format attachment and
  returns HTTP 202, but does not return a task ID.
- `GET /api/v1/tasks` returns task IDs, numeric status values, task messages,
  progress, and the CWNG display name associated with each task.
- `POST /api/v1/books/{book_id}/metadata` supports selected catalogue
  metadata fields, identifiers, and editable custom columns. The current book
  detail serializer exposes custom-column values only for columns that already
  have a value; page count is therefore projected only when a clearly named
  integer custom column is already populated.
- CWNG's metadata editor accepts language display names in the active CWNG
  locale. The integration currently does not attempt to translate LibrisLog's
  ISO language code into that locale-specific display name.

## Authentication and security constraints

CWNG's HTTP Basic/app-password allow-list does not include the upload routes.
Uploads require a normal authenticated session, account upload permission, and
the server-level uploads-enabled setting. Metadata updates additionally require
the CWNG account's edit permission.

The integration therefore:
- keeps CWNG credentials, cookies, and CSRF tokens out of the frontend;
- encrypts the password and reusable session cookie at rest using
  `API_KEY_ENCRYPTION_KEY`;
- refreshes the CSRF token when opening a session and reuses the cookie-backed
  session to avoid CWNG's login rate limits;
- disables redirects on credential-bearing HTTP requests;
- requires a public HTTPS hostname and rejects hosts resolving to non-public IPs;
- resolves the configured hostname again when opening a CWNG session, validates
  the complete result set, and pins the HTTP transport to those exact IP
  addresses. The original hostname is retained for TLS SNI and certificate
  verification. Unexpected destinations fail closed. This uses the
  HTTPX/httpcore network-backend hook and is covered by a unit test; keep the
  supported HTTPX/httpcore major versions compatible with this transport;
- checks EPUB/PDF signatures and enforces the 100 MB upload limit;
- never changes the Kobo `api_endpoint` token or any Kobo configuration.

Backups must include the LibrisLog `API_KEY_ENCRYPTION_KEY`; losing it makes
stored CWNG credentials and session cookies undecryptable.

## Remaining work before deployment

- Validate the pinned DNS transport against the deployed runtime and
  reverse-proxy route before enabling a real CWNG connection.
- Expand route-level authorization/isolation tests and concurrent-upload tests
  around uploads and explicit mappings.
- Run the full backend, frontend, CLI, and Playwright test suites on the branch.
- Validate a real connection, upload, task poll, explicit match, metadata sync,
  format replacement, and download against a non-production CWNG account.
- Confirm the CWNG deployment has uploads enabled and the mapped account has
  both upload and edit permissions.
- Consider a dedicated least-privilege CWNG account. Do not reuse or alter
  Katrien's existing Kobo account/token as part of this integration.
- Add language and cover sync only after their CWNG write paths and data
  conversions are verified.

No deployment, merge, or changes to the live homelab are part of this branch.
