- **Money goes through the decimal helper.** Never `parseFloat`/`Number()` a
  price, quantity, fee or rate.
- **Colors come from tokens.** No hex outside the tokens file.
- **`server-only`** at the top of any module reading `process.env` or the
  database.
- **Both shells.** A UI change lands on mobile and desktop in the same pass.
- **Migrations are generated, never hand-edited, and additive.**
