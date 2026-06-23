# Weekly Sync Context Handover
handover_id: "weekly-sync"
name: "Weekly Sync Context"
description: "Passing context from one week to agents for next week"

trigger: "Friday end of week"

template: |
  Team sync context:

  Week [N] Progress:
  - ML Engineer: [What was accomplished]
  - Backend Engineer: [What was accomplished]
  - DevOps Lead: [What was accomplished]
  - Architect: [Assessment of week]

  Metrics:
  - Model accuracy: [X]%
  - API latency: [X]ms
  - Test coverage: [X]%
  - Infrastructure status: [Status]

  Blockers:
  - [Blocker 1]
  - [Blocker 2]

  Week [N+1] Focus:
  - ML Engineer will: [Task]
  - Backend Engineer will: [Task]
  - DevOps Lead will: [Task]
  - Architect will: [Task]

  Questions for next week:
  - [Question 1]
  - [Question 2]

agents_to_receive:
  - "ml-engineer"
  - "backend-engineer"
  - "devops-lead"
  - "architect"

timing: "Monday morning before work starts"
