1. **No float money math.** Database `numeric` → string → a decimal library
   through one money module. `parseFloat`/`Number()` on a price, quantity, fee
   or rate is a bug. *(preflight BLOCK, verifier grep)*
2. **No hardcoded colors.** One tokens file is the only file allowed a color
   literal; everything else uses the generated utilities. *(preflight WARN,
   verifier grep)*
3. **Both shells, same pass.** Any UI change handles mobile and desktop
   together, branching in code — never CSS-hiding a duplicate tree.
   *(preflight BLOCK)*
4. **Third-party data is server-mediated.** A client component never calls a
   vendor API directly. *(preflight BLOCK, verifier grep)*
5. **Auth gates stay intact.** Every allowlist or role check in the auth module
   survives every change; a new sign-in method passes the same gates.
   *(security reviewer)*
6. **`server-only` on anything reading `process.env`** or the database.
   *(verifier grep, security reviewer)*
7. **Migrations are additive.** They run before the new code deploys, so a
   destructive change is split across two deploys. *(preflight BLOCK)*
8. **Never commit or push** unless explicitly asked.
