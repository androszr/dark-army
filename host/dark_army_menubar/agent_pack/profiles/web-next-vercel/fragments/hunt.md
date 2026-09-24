1. **Decimal safety** — a `number` in a money path; `.toNumber()` feeding back
   into arithmetic rather than into a formatter; a `numeric` column read
   without the decimal helper; rounding applied twice.
2. **Aggregation correctness** — out-of-order records, a subtraction that
   exceeds what is held, zero-quantity rows left in a list, a rate applied at
   the wrong date (current instead of the stored one).
3. **Query-cache misuse** — a query key missing a variable it depends on
   (stale data across a switch), a refetch interval running while the tab is
   hidden, mutations not invalidating.
4. **RSC / client boundary** — a `server-only` module pulled into a client
   tree; a secret reaching the bundle; an `async` component used as a client
   component; a Server Action missing input validation.
5. **Upstream failover** — the primary error path not actually falling through
   to the fallback; a partial batch treated as a total failure; freshness
   metadata not stamped, so the UI lies about how old the data is.
6. **Timezone** — business-hours logic using local time instead of the
   relevant zone; a `date` column round-tripped through a `Date` and shifting
   a day.
7. **Breakpoint coverage** — a feature that only exists in one shell; a tap
   target under 44px; content hidden behind the home indicator.
8. **Accessibility** — meaning conveyed by color alone; missing `aria-current`;
   a removed focus ring; an icon-only control with no accessible name.
