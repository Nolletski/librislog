# Calibre-Web NextGen API compatibility

Validated against the upstream `v4.1.74` tag of
`new-usemame/Calibre-Web-NextGen`.

## Confirmed API behaviour

- `GET /api/v1/auth/csrf` returns a CSRF token.
- `POST /api/v1/auth/login` accepts the account username and password and
  establishes a normal CWNG login session.
- `GET /api/v1/auth/me` returns the authenticated account and feature flags.
- `POST /api/v1/upload` accepts one or more multipart `file` fields and
  queues new files for asynchronous ingest. It returns queued filenames and
  per-file errors, not a Calibre book ID or ingest task ID.
- `POST /api/v1/books/{book_id}/formats` queues a format for an existing
  book and returns HTTP 202. It also does not return a processing task ID.
- `GET /api/v1/tasks` lists the current user's tasks. Each task entry includes
  `task_id`, numeric `stat`, localized `status`, `taskMessage`, progress,
  and optional error text. This can support task polling, but the task must be
  correlated with the upload without relying on localized display strings.

## Authentication constraint

The API's native HTTP Basic/app-password allow-list in `cps/api/__init__.py`
does **not** include `upload_books` or `add_format`. The upload routes also
check `current_user.role_upload()`. Therefore a Basic app password is not
sufficient for upload, even though it can authenticate selected read endpoints.

The supported integration path for uploads is a normal CWNG login session:
obtain a CSRF token, log in with the mapped CWNG account, then use the returned
session cookie and CSRF token for upload requests. The account must have upload
permission and uploads must be enabled server-side.

## Integration implications

- Keep credentials server-side only; never expose them to the browser.
- Do not reuse or alter the Kobo `api_endpoint` token.
- Use a dedicated, least-privilege CWNG account for automation where practical.
  Map each LibrisLog user explicitly to a CWNG account; do not infer identity
  from matching email addresses.
- Treat uploads as asynchronous. The upload response alone cannot establish
  the permanent LibrisLog-to-Calibre book mapping.
- Do not match or persist mappings based solely on ISBN/title. They are hints
  for candidate resolution, not durable identifiers.
- Do not delete files/books in CWNG when a LibrisLog record is deleted.

## Still to validate before implementing upload orchestration

- Whether a newly created upload task can be correlated reliably with a specific
  upload request (the task list has IDs/status but no explicit upload request ID).
- Whether a newly ingested book can be safely resolved through a scoped
  library query and verified metadata/format checks without ambiguous matches.
- Exact CSRF/session cookie behaviour for server-to-server HTTP clients,
  including login, session renewal, logout, and reverse-proxy deployments.
- Whether a CWNG account can be restricted to the required library and upload
  permissions without changing the existing Kobo account configuration.


## LibrisLog credential storage finding

The LibrisLog backend already has an established at-rest encryption mechanism:
- `backend/app/auth.py` exposes `encrypt_api_key()` and `decrypt_api_key()`,
  implemented with Fernet and a SHA-256-derived key.
- The key comes from the required `API_KEY_ENCRYPTION_KEY` setting.
- The existing `ApiKey.key_encrypted` field demonstrates the intended storage
  pattern for recoverable secrets.

The CWNG password can therefore use this existing encryption primitive rather
than introducing a second key or custom cryptography. The plaintext password
must only exist transiently in the backend process; it must never be returned
by an API response, included in logs, or sent to the frontend. A connection
test should report only success or a sanitized failure reason. Key rotation and
backup/restore of `API_KEY_ENCRYPTION_KEY` remain operational requirements:
losing the key makes stored CWNG credentials undecryptable.

## Session and upload details confirmed in source

Further inspection of the pinned CWNG source confirms:
- `GET /api/v1/auth/csrf` calls Flask-WTF's `generate_csrf()`.
- `POST /api/v1/auth/login` authenticates a normal CWNG account and calls
  Flask-Login's `login_user()`, establishing the cookie-backed session.
- The login endpoint has rate limits (3 per minute and 40 per day per rate-limit
  key); the integration must reuse a session rather than logging in for every
  upload.
- Uploads require an authenticated non-anonymous account, `role_upload()`,
  and the server-level uploads-enabled setting.
- `POST /api/v1/upload` creates an ingest sidecar manifest, queues a
  `TaskUpload`, and returns only queued filenames and per-file errors.
- `POST /api/v1/books/{book_id}/formats` queues a format attachment and
  returns the queued filename with HTTP 202.
- The Basic-auth read allow-list includes `/api/v1/tasks`, but the upload
  endpoints are not in the Basic-auth write allow-list. A Basic app password
  therefore cannot perform uploads.

## Implementation boundary

The first implementation should use a backend-only CWNG client with a
persistent cookie jar and CSRF handling. It should:
1. Fetch a CSRF token and authenticate once using the explicitly mapped CWNG
   account, then reuse the resulting session for subsequent requests.
2. Check `/api/v1/auth/me` and the server's `uploading` feature before
   enabling upload actions.
3. Upload only EPUB/PDF initially (subject to the CWNG server's allowed-format
   configuration), with size limits and MIME/extension validation in LibrisLog.
4. Treat the response as "queued", not "imported". Resolve the final CWNG book
   ID only after ingest has completed and a candidate has been verified.
5. Keep the durable mapping separate from the `Book` record and scoped to the
   LibrisLog user plus CWNG connection. Never use ISBN as the mapping key.
6. Never synchronize reading status, rating, reading sessions, or progress to
   CWNG in this first phase; never delete a CWNG book when a LibrisLog book is
   deleted.

Task correlation is still a blocker for safe automatic resolution. The upload
route creates a task internally but does not return its task ID. A later
implementation must prove a deterministic correlation strategy (for example,
a unique upload marker/filename plus before-and-after task/library inspection)
and handle concurrent uploads. If that cannot be made reliable, the first
release should expose a queued state and require explicit user confirmation of
the matching CWNG book instead of guessing.
