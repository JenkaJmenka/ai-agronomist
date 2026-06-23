# ADR-003: Modular Architecture for Easy Phase 1.5 Scaling

## Status

ACCEPTED

## Context

Phase 1: Single model, single customer
Phase 1.5: Model adaptation infrastructure, 3-5 customers with different crops/conditions
Phase 2: Multi-customer platform with vendor integration

Must design Phase 1 to avoid major refactoring at Phase 1.5.

## Problem

How to structure Phase 1 code to enable Phase 1.5 multi-customer scaling?

## Decision

Three design principles:

1. **Separated Concerns**
   - Models (src/models/) independent of data/API
   - Data loading (src/data/) independent of model
   - Database (Phase 2) independent of model

2. **Configuration-Driven**
   - No hardcoded paths, hyperparameters in config.py
   - Easy to adjust for different greenhouse types
   - Environment variables for deployment flexibility

3. **Pluggable Components**
   - Data generator can be swapped for real data loader (Phase 1.5)
   - Training script can handle multiple models (Phase 1.5)
   - Database schema extensible (Phase 2 additions)

## Rationale

1. **Minimal Refactoring**: <20% of Phase 1 work at Phase 1.5
2. **Reusability**: Base model can transfer to new customers
3. **Testability**: Each component independently testable
4. **Maintainability**: Clear interfaces between components

## Consequences

- Slightly more abstraction than needed for Phase 1
- More files/classes (vs monolithic script)
- Configuration management overhead (mitigated by defaults)

## Code Structure

```
src/
├── models/           # Model definitions (same for all customers)
│   ├── lstm_vae.py
│   ├── data_generator.py  # Swappable with real loader
│   └── training.py
├── data/             # Data loading (will expand Phase 1.5)
│   └── csv_loader.py
└── config.py         # All hardcoded values here

Phase 1.5 additions:
├── data/
│   ├── priva_loader.py         # NEW: Priva API integration
│   └── greenhouse_adapter.py    # NEW: Vendor-agnostic adapter
├── models/
│   └── model_adapter.py         # NEW: Transfer learning
└── database/         # NEW: Customer data storage
```

## Phase 1.5 Refactoring Budget

- Data loading: 30% new code, 0% existing code changes
- Model adaptation: 20% new code, 5% existing code changes
- Database layer: 50% new code, 0% existing code changes
- Total: ~<20% effort relative to Phase 1
