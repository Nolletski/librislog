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
