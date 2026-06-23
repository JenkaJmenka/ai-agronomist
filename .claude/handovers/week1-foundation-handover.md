# Week 1-2 Foundation Handover
handover_id: "week1-foundation"
name: "Week 1-2 Foundation Handover"
description: "ML + Backend + DevOps collaborate on foundation"

context: |
  Week 1-2: Foundation Phase

  ML Engineer is building:
  - LSTM-VAE model
  - Synthetic data generator
  - Training pipeline

  Backend Engineer needs:
  - Model input/output specifications
  - Data shape and format

  DevOps Lead needs:
  - Python dependencies
  - Memory/compute requirements

  Architect reviews:
  - Model architecture
  - Data schema
  - Integration points

dependencies:
  - ML provides: Model input/output specs
  - Backend provides: Schema design
  - DevOps provides: Compute requirements

handover_points:
  day_2:
    from: "ml-engineer"
    to: "backend-engineer"
    content: "Model input/output specs"

  day_3:
    from: "backend-engineer"
    to: "devops-lead"
    content: "Python dependencies list"

  day_4:
    from: "all"
    to: "architect"
    content: "Design review and ADRs"
