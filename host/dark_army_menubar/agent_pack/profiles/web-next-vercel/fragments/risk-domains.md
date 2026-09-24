- **Money is never a float.** Database `numeric` → string → decimal through
  the money module. A `parseFloat` or `Number()` on a price, quantity, fee or
  rate is a bug, full stop. **Lead with this one**: the damage is silent and
  cumulative. Plain-language framing: "the numbers would be slightly wrong in
  a way nobody notices until they do not add up."
- **Third-party data is server-mediated.** A client component calling a vendor
  directly is both a key-exposure and a rate-limit problem in one.
- **The auth gates stay intact.** Any change that widens who can get in is the
  most serious thing a review here can find.
- **`server-only` on anything reading `process.env` or the database.** Its
  absence is how a secret reaches the client bundle.
- **Migrations are additive.** They run *before* the new code deploys, so a
  destructive change has to be split across two deploys. A single-deploy
  destructive migration is a production outage with data loss.
- **Secrets and injection.** Keys in the client bundle, unparameterised SQL,
  unvalidated input reaching a vendor call.
- **Cookie flags, CSP and headers.** Regressions here are invisible until they
  are exploited.
- **Both shells, one pass.** A UI change handling only mobile or only desktop.
- **No hardcoded colors.** The tokens file is the only file allowed a literal.
