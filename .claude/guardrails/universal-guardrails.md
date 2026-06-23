# Universal Guardrails

## STOP WORK — never proceed if found
- Tight coupling between LSTM-VAE and API layer
- Secrets or API keys hardcoded in source files
- Model checkpoint saved without metadata (epoch, loss, date, config)
- Database schema without tenant_id field
- Test coverage below 80% on critical paths
- Major design change without ADR in docs/architecture/

## WARN — log and continue
- Hard-coded thresholds (should be in src/config.py)
- Missing error handling on external calls
- Code duplication (3+ identical blocks)
- Silent failures (bare except, pass)
