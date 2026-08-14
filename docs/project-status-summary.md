# What's happening with the analytics tool — plain-English update

*(2026-08-14. Technical version: [project-status-technical.md](project-status-technical.md))*

## The short version

We just fixed the two biggest problems the tool had. One more small piece of
older work is on hold for a good reason, and three more chunks of planned
work haven't started yet.

## Problem 1 (FIXED): customers were sharing a filing cabinet

Every customer using this tool is a different company. Their data is
supposed to be completely separate — like each company having its own
locked filing cabinet.

**What was actually happening:** everyone's data was in one shared filing
cabinet, separated only by a sticky note on each folder saying which company
it belonged to. If someone forgot to check the sticky note, or a setting was
wrong, two companies' data could get mixed together. That's a real risk for
any business handling other companies' information.

**What we did:** every company now gets its own actual, separate filing
cabinet (its own database file). There's no sticky note to forget — it's
physically impossible to open Company A's cabinet and see Company B's papers,
because they're not in the same room anymore. Deleting a company's data,
backing it up, or exporting it is now a single, clean operation instead of
carefully filtering through a shared pile.

## Problem 2 (FIXED): the tool's "memory" never actually worked

This tool has a "Brain" — a memory of approved facts, past questions, and
business rules that the AI assistant is supposed to search through before
answering a question, so it gives grounded, trustworthy answers instead of
guessing.

**What was actually happening:** the search feature was broken. Every time
the assistant tried to search its memory, it hit an error that was silently
swallowed — nobody saw it fail. So it fell back to a dumb, literal
word-match that essentially never found anything. The Brain could have
thousands of good facts stored in it, and the assistant would still act like
it knew nothing. This was the single biggest reason the assistant gave
generic or wrong answers instead of using what the company had already
taught it.

**What we did:** rebuilt the search from scratch using two complementary
techniques — one that matches exact words and jargon (like product codes or
city names), and one that understands *meaning*, so a rephrased question
still finds the right answer. The two are combined and ranked together. We
also caught and fixed a subtle bug in that ranking during final testing: a
node marked "extra trustworthy" could occasionally jump ahead of the actual
best answer and bump it out of the results entirely — fixed and tested
before this went live. Along the way we removed a piece of complicated,
now-unneeded infrastructure (a separate vector-search product) entirely,
making the system simpler, not just fixed.

**Bottom line:** the assistant can now actually find and use the knowledge
your company has approved, for the first time.

## What's built but not yet merged in, and why

There's one piece of older work — a feature branch someone (or an earlier
session) started, called **PR #3** — that adds two nice things: (1) the
assistant getting smarter about figuring out what you're really asking
before it searches, and (2) letting it write custom SQL from approved
context instead of only reusing an exact old query.

**Why it's not merged:** it was built against the *old* filing-cabinet
system, before Problem 1 was fixed. Now that the plumbing underneath it has
changed, its code doesn't fit anymore — like a drawer built for the old
cabinet that doesn't slide into the new one. Rather than force it in badly,
the plan has always been: wait until the new search system (Problem 2) was
done, then carefully lift out just the two useful ideas and rebuild them
properly on the new foundation. That waiting condition is now met — this is
simply next on the list, not stuck or abandoned.

## What's next

Three more planned projects, none started yet:

1. **Making sure only approved knowledge counts as fact.** Right now a few
   places let information sneak into the Brain without a human actually
   signing off on it — the opposite of how it's supposed to work. This
   project locks that down.
2. **Making the "skills" feature work for every customer, not just one.**
   There's a reusable analytical shortcut (a "skill") that's currently
   wired to one specific company's table names — so it silently fails for
   everyone else. This project makes it generic, so every company gets the
   benefit without engineering it can be improved once for everyone.
3. **Teaching the assistant which facts to trust more.** Not everything the
   Brain knows is equally fresh or reliable — this project makes old
   information visibly "decay" over time so stale facts stop being weighted
   the same as current ones.

Plus: finishing the PR #3 port described above.

None of these are urgent blockers — the tool works correctly without them.
They're improvements to trust and correctness on top of a now-working
foundation.
