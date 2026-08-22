# CLAUDE.md

This project keeps a single set of agent instructions, and they live in
[AGENTS.md](AGENTS.md).

**Read `AGENTS.md` before doing anything in this repository.** Everything that
governs work here is in that file: engineering and architecture standards,
multi-tenancy and isolation rules, operational governance, how to communicate
with the repository owner, and the frameworks the Stakeholder Analyst and Junior
Analyst personas must embed.

There is deliberately no separate copy of those rules here. One file is the
source of truth, so a rule cannot be updated in one place and quietly go stale in
another. `GEMINI.md` exists for the same reason and points at the same file.

If you are adding or changing a rule, change it in `AGENTS.md`.
