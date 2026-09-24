## A. Surface

> Where does this live?

- New screen (a new route)
- Extends an existing screen — which one?
- A shared component used by several screens
- Server-side only (engine, provider, API route) — no UI

## B. Data source

> Where does the data come from?

- Existing database tables, read via a Server Component
- Needs a schema change (new column/table → migration)
- A third-party service, fetched on the server
- Derived in code from data already loaded

## C. Scope

*Ask only when the idea could apply to one record or to all of them.*

> Does this operate on one item or on the whole set?

- The selected item only
- The merged "all" view only
- Both — and they must agree
- Neither; it is independent

## D. Mutation shape

> Does the user change data here?

- Read-only
- A Server Action creating or updating one row
- Bulk or multi-row change (needs a transaction)

## E. Freshness

*Ask only when a number on screen has to stay current.*

> How should this stay up to date?

- Fetched once per page load
- Refetched on an interval, gated on tab visibility (and business hours if any)
- Pushed from the server
- It does not change once shown

## F. Priority when the trade-off is real

*Ask only when the idea implies a genuine tension.*

> If these conflict, which wins?

- Match the reference product's behaviour
- Fewer taps on mobile
- More information density on desktop
