- **Money never touches a float.** Name the decimal helper every money step
  goes through, and never plan a `parseFloat`/`Number()` on a price, quantity,
  fee or rate.
- **Both shells in the same plan.** A UI step says what mobile and desktop
  each do, branching in code, never by hiding a duplicate tree.
- **Data from a vendor is fetched on the server.** A plan that has a client
  component call a vendor is wrong by construction — route it through the
  server contract that already exists.
- **Security-relevant plans name the reviewer.** Anything touching auth, the
  proxy, an API route, the env schema, headers or dependencies lists the
  security reviewer in its `Stages:` line and in a step.
- **Migrations are additive and split.** A drop, rename or NOT NULL is two
  deploys: stop writing → deploy → drop. Say which deploy each half is in.
