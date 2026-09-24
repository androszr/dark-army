- **The deployed environment.** Env-schema validation runs at build time on
  Vercel with the project's real variables; CI builds with placeholders. A
  variable missing from Vercel fails the deploy, never the tests.
- **The client bundle.** Nothing in the suite greps `.next/static/` for a
  secret that leaked through a missing `server-only`.
- **Security headers and cookies.** CSP, HSTS, cookie flags are set by config
  and only observable on a live URL.
- **The migration order.** Tests run against a schema that already matches
  the code; production runs the migration first against the still-live old
  code.
- **A third-party response that changes shape.** Fixtures are frozen; the
  vendor is not.
