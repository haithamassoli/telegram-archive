This project uses [Convex](https://convex.dev) as its backend, but **the Convex
source does not live here.** Schema and functions live in the site repo,
`../kashaf-alkulify/convex/`, which owns the shared `alkulify` deployment. This
repo is a client: `src/archive/convex.py` calls the deployment over its HTTP
API using `CONVEX_URL`.

Never create a `convex/` directory here and never run `npx convex dev` from
this repo. A Convex push replaces the deployment's entire function set and
schema, so a second `convex/` directory pointed at the same deployment deletes
the other one's backend.

When working on Convex code, do it in `../kashaf-alkulify/` and **always read
`convex/_generated/ai/guidelines.md` there first** — it contains rules that
override what you may have learned about Convex from training data.
