# Project Instructions

## Agent skills

### Issue tracker

Issues and PRDs live in GitHub Issues for `ganluCoding/etf_t-0_hk_stock`. See `docs/agents/issue-tracker.md`.

### Triage labels

The repository uses the canonical triage labels recorded in `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. Read `CONTEXT.md` and relevant files in `docs/adr/` before changing research, data, cost, execution, or risk behavior. See `docs/agents/domain.md`.

## Working rules

- This repository supports research and manual trading decisions only. Never add broker order submission, credential storage, or autonomous trading.
- Treat “套利” as a claim requiring a hedge or a structural convergence mechanism. The current scope is ETF intraday grid / mean-reversion research.
- Keep source code, documentation, metadata, tests, and small fixtures in Git.
- Keep raw market data, broker statements, cached API responses, and generated large datasets out of Git. Use DVC pointers for versioned datasets and retain their working copies locally.
- Do not commit secrets, account identifiers, API keys, tokens, or unredacted broker statements.
- Every research change must preserve causal signal timing, full transaction costs, inventory constraints, and mark-to-executable-price equity accounting.
- Use GitHub Issues for scoped work. Link commits and pull requests to their issue numbers.

