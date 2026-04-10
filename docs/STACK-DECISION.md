# STACK-DECISION.md — Phase 0

## Decision

The first implementation of Deliberation Room will use **Python 3.12 + UV** for the Room Engine and the CLI Shell.

## What This Optimizes For

This choice optimizes for **MVP learning speed**.

The goal of the MVP is to answer a product question first: is Deliberation Room a genuinely useful and necessary tool, or is it only an interesting experiment?

## Why Python First

- Fastest path to a usable headless engine plus CLI-first workflow
- Lowest friction for multi-provider LLM integration and adapter iteration
- Strong fit for JSON-first state handling, local filesystem persistence, and test-first development
- High coding-agent effectiveness during a greenfield build

## Post-MVP Direction

If the MVP validates that the product is genuinely useful, the leading next step is an **early reimplementation of the Room Engine in Go** before broad product-feature expansion.

The intent is to make the switch early, while the system is still small, rather than carrying a Python MVP too far into productization and paying a larger rewrite cost later.

This is a likely post-MVP direction, not an automatic commitment. The final decision will be made after MVP evaluation.

## Constraints For MVP Implementation

- Keep the Room Engine framework-free
- Preserve language-agnostic module boundaries and data contracts
- Keep the CLI shell thin and delegate all business logic to the engine
- Avoid Python-specific architectural lock-in that would make a later Go rewrite harder
