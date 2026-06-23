# IMPLEMENTATION PLAN: Gap 2 - Incident Memory System (pgvector)
## Timeline: 2 days | Priority: TIER 1 - CRITICAL

---

## 🎯 OBJECTIVE
Build semantic search over past anomalies using PostgreSQL + pgvector. Enable queries like:
- "Find all anomalies similar to this one"
- "What were the root causes of similar failures?"
- "What actions fixed similar problems?"

**Problem Solved:**
- Currently: Anomalies are detected but not remembered
- After: System learns from history, suggests fixes based on past incidents

---

## 📊 IMPACT
- ✅ "This looks like the May 15th boiler failure" (operator context)
- ✅ Suggests root cause + recommended actions
- ✅ Reduces MTTR (mean time to resolution)
- ✅ Foundation for automated anomaly classification

---

## 🏗️ FILE STRUCTURE

```
src/db/
├── models.py                    (update with Incident table)
├── incident_memory.py           ⭐ NEW - Main memory system
└── migrations/
    └── 003_add_incident_memory_tables.py ⭐ NEW

src/api/routes/
├── incidents.py                 ⭐ NEW - API endpoints for incident search
└── anomalies.py                 ⭐ NEW - Link anomalies to incidents

tests/
├── db/test_incident_memory.py   ⭐ NEW
└── api/test_incident_api.py     ⭐ NEW

docs/
└── incident_memory_schema.md    ⭐ NEW
```

---

## 🔧 IMPLEMENTATION PLAN

### PREREQUISITE: Set up PostgreSQL + pgvector

```bash
# 1. Install PostgreSQL (if not already)
# Ubuntu:
sudo apt-get install postgresql postgresql-contrib

# 2. Install pgvector extension
sudo apt-get install postgresql-pgvector

# 3. Create database and enable extension
psql -U postgres
CREATE DATABASE ai_agronomist_prod;
\c ai_agronomist_prod
CREATE EXTENSION IF NOT EXISTS vector;

# 4. Update connection string in .env
DATABASE_URL=postgresql://user:password@localhost:5432/ai_agronomist_prod
```

---

### STEP 1: Update `src/db/models.py`

Add Incident table with pgvector support:

```python
"""
Updated database models with pgvector support.
"""

from sqlalchemy import Column, String, Float, DateTime, Integer, Boolean, JSON, func, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from pgvector.sqlalchemy import Vector
from datetime import datetime
import uuid

from src.db.session import Base


class Incident(Base):
    """
    Stores detected anomalies with metadata and root cause analysis.
    
    pgvector enables semantic search: find similar past anomalies.
    """
    __tablename__ = "incidents"
    
    # =====================================================================
    # PRIMARY KEYS & IDENTIFIERS
    # =====================================================================
    
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique incident ID"
    )
    customer_id = Column(
        String(256),
        index=True,
        nullable=False,
        comment="Which customer's greenhouse"
    )
    
    # =====================================================================
    # TEMPORAL METADATA
    # =====================================================================
    
    detected_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True,
        comment="When anomaly was first detected"
    )
    resolved_at = Column(
        DateTime,
        nullable=True,
        comment="When incident was resolved (null=still open)"
    )
    reported_at = Column(
        DateTime,
        nullable=True,
        comment="When operator reported the issue"
    )
    
    # =====================================================================
    # ANOMALY DATA
    # =====================================================================
    
    # Raw sensor sequence (stored as JSON for later analysis)
    sequence = Column(
        JSONB,
        nullable=False,
        comment="The sensor readings that triggered anomaly [seq_len, 4]"
    )
    
    # Time series metadata
    sequence_length = Column(
        Integer,
        default=20,
        comment="Number of timesteps in sequence"
    )
    
    # Anomaly scores from detection pipeline
    anomaly_score = Column(
        Float,
        nullable=False,
        comment="Reconstruction error from LSTM-VAE [0, ∞)"
    )
    
    # ⭐⭐⭐ CRITICAL: Vector embedding from latent space
    embedding = Column(
        Vector(8),  # 8-dimensional latent space
        nullable=False,
        comment="8-dim embedding from LSTM-VAE latent space"
    )
    
    # Uncertainty of the embedding
    embedding_uncertainty = Column(
        Float,
        nullable=False,
        comment="Std dev in latent space (0-1), higher=more uncertain"
    )
    
    # =====================================================================
    # ROOT CAUSE ANALYSIS
    # =====================================================================
    
    # Operator-provided or automatically inferred root cause
    root_cause = Column(
        String(256),
        nullable=True,
        index=True,
        comment="Inferred root cause: 'boiler_failure', 'power_glitch', etc."
    )
    
    root_cause_confidence = Column(
        Float,
        nullable=True,
        comment="Confidence in root cause diagnosis (0-1)"
    )
    
    root_cause_details = Column(
        JSONB,
        nullable=True,
        comment="Details: {sensor_divergence: 'temperature', first_change_time: 5, ...}"
    )
    
    # =====================================================================
    # RESOLUTION & ACTIONS
    # =====================================================================
    
    resolved = Column(
        Boolean,
        default=False,
        index=True,
        comment="Has this incident been resolved?"
    )
    
    actions_taken = Column(
        JSONB,  # List of strings: ['restart_heating', 'increase_fan', ...]
        nullable=True,
        comment="What was done to fix it?"
    )
    
    actions_effective = Column(
        Boolean,
        nullable=True,
        comment="Did the actions fix the problem?"
    )
    
    effectiveness_score = Column(
        Float,
        nullable=True,
        comment="How quickly was it resolved? (0-1, 1=instant)"
    )
    
    # =====================================================================
    # IMPACT METRICS
    # =====================================================================
    
    time_to_detect = Column(
        Integer,
        nullable=True,
        comment="Minutes from problem onset to detection"
    )
    
    time_to_resolve = Column(
        Integer,
        nullable=True,
        comment="Minutes from detection to resolution"
    )
    
    crop_damage_estimated = Column(
        Float,
        nullable=True,
        comment="Estimated crop loss % (0-100)"
    )
    
    # =====================================================================
    # ADDITIONAL METADATA
    # =====================================================================
    
    tags = Column(
        JSONB,  # List of strings: ['urgent', 'recurring', 'hardware_failure']
        default=list,
        comment="User-assigned tags for filtering"
    )
    
    notes = Column(
        String(1024),
        nullable=True,
        comment="Operator notes/observations"
    )
    
    # =====================================================================
    # INDEXES (CRITICAL FOR PERFORMANCE)
    # =====================================================================
    
    __table_args__ = (
        # Vector similarity search index (ivfflat for large datasets)
        Index("ix_embedding_ivfflat", embedding, postgresql_using="ivfflat"),
        
        # Common filters
        Index("ix_customer_detected", customer_id, detected_at),
        Index("ix_resolved_status", resolved, detected_at),
        
        # Root cause lookup
        Index("ix_root_cause", root_cause),
    )
    
    def __repr__(self):
        return f"<Incident {self.id} customer={self.customer_id} cause={self.root_cause}>"


class IncidentAction(Base):
    """
    Individual action taken during incident resolution.
    
    Example: incident_id=123, action='increase_ventilation', effectiveness=True
    """
    __tablename__ = "incident_actions"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    incident_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    
    # What was done
    action = Column(String(256), nullable=False)  # 'restart_heating', 'increase_fan'
    action_time = Column(DateTime, default=datetime.utcnow)
    
    # Did it work?
    effectiveness = Column(Boolean, nullable=True)  # True=fixed, False=made worse, None=no effect
    notes = Column(String(512), nullable=True)
```

---

### STEP 2: Create Database Migration

`src/db/migrations/versions/003_add_incident_memory_tables.py`:

```python
"""Add incident memory tables with pgvector."""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = '003'
down_revision = '002'


def upgrade():
    """Create incident memory tables."""
    
    # Create vector extension (if not exists)
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    
    # Create incidents table
    op.create_table(
        'incidents',
        sa.Column('id', sa.UUID(as_uuid=True), primary_key=True, default=sa.func.gen_random_uuid()),
        sa.Column('customer_id', sa.String(256), nullable=False, index=True),
        
        # Temporal
        sa.Column('detected_at', sa.DateTime, default=sa.func.now(), nullable=False, index=True),
        sa.Column('resolved_at', sa.DateTime, nullable=True),
        sa.Column('reported_at', sa.DateTime, nullable=True),
        
        # Anomaly data
        sa.Column('sequence', sa.JSON, nullable=False),
        sa.Column('sequence_length', sa.Integer, default=20),
        sa.Column('anomaly_score', sa.Float, nullable=False),
        
        # ⭐ Vector embedding
        sa.Column('embedding', Vector(8), nullable=False),
        sa.Column('embedding_uncertainty', sa.Float, nullable=False),
        
        # Root cause
        sa.Column('root_cause', sa.String(256), index=True),
        sa.Column('root_cause_confidence', sa.Float),
        sa.Column('root_cause_details', sa.JSON),
        
        # Resolution
        sa.Column('resolved', sa.Boolean, default=False, index=True),
        sa.Column('actions_taken', sa.JSON),
        sa.Column('actions_effective', sa.Boolean),
        sa.Column('effectiveness_score', sa.Float),
        
        # Impact
        sa.Column('time_to_detect', sa.Integer),
        sa.Column('time_to_resolve', sa.Integer),
        sa.Column('crop_damage_estimated', sa.Float),
        
        # Metadata
        sa.Column('tags', sa.JSON, default=list),
        sa.Column('notes', sa.String(1024)),
    )
    
    # Create indexes
    op.create_index('ix_embedding_ivfflat', 'incidents', ['embedding'], 
                    postgresql_using='ivfflat')
    op.create_index('ix_customer_detected', 'incidents', ['customer_id', 'detected_at'])
    op.create_index('ix_resolved_status', 'incidents', ['resolved', 'detected_at'])
    op.create_index('ix_root_cause', 'incidents', ['root_cause'])
    
    # Create incident_actions table
    op.create_table(
        'incident_actions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('incident_id', sa.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('action', sa.String(256), nullable=False),
        sa.Column('action_time', sa.DateTime, default=sa.func.now()),
        sa.Column('effectiveness', sa.Boolean),
        sa.Column('notes', sa.String(512)),
    )


def downgrade():
    """Drop incident memory tables."""
    op.drop_table('incident_actions')
    op.drop_table('incidents')
```

---

### STEP 3: Create `src/db/incident_memory.py`

Main incident memory system:

```python
"""
Incident Memory System: Store and search past anomalies using pgvector.

Core capability:
- Store detected anomalies with metadata
- Search for similar past incidents using vector similarity
- Suggest root causes and fixes based on history
"""

import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import numpy as np
from sqlalchemy import and_, or_, desc, func
from sqlalchemy.orm import Session

from src.db.models import Incident, IncidentAction
from src.db.session import get_db

logger = logging.getLogger(__name__)


class IncidentMemory:
    """
    Semantic search over past incidents.
    
    Example usage:
        memory = IncidentMemory(db_session)
        
        # Store new incident
        memory.store_incident(
            customer_id="greenhouse_001",
            embedding=np.array([...]),  # 8-dim from LSTM-VAE
            anomaly_score=0.85,
            sequence=sensor_data,
            ...
        )
        
        # Find similar past incidents
        similar = memory.find_similar(
            embedding=new_embedding,
            customer_id="greenhouse_001",
            k=5
        )
        
        # Get suggestions
        suggestions = memory.suggest_fixes(embedding=new_embedding)
    """
    
    def __init__(self, db_session: Session):
        self.db = db_session
    
    # =========================================================================
    # STORE INCIDENTS
    # =========================================================================
    
    def store_incident(self,
                      customer_id: str,
                      embedding: np.ndarray,
                      anomaly_score: float,
                      sequence: np.ndarray,
                      embedding_uncertainty: float = 0.1,
                      root_cause: Optional[str] = None,
                      **metadata) -> str:
        """
        Store a detected anomaly as an incident.
        
        Args:
            customer_id: Greenhouse identifier
            embedding: 8-dimensional latent vector
            anomaly_score: Reconstruction error [0, ∞)
            sequence: Sensor readings (seq_len, 4)
            embedding_uncertainty: Std dev in latent space
            root_cause: Optional root cause label
            **metadata: Additional fields (tags, notes, etc.)
        
        Returns:
            incident_id: UUID of stored incident
        
        Example:
            >>> incident_id = memory.store_incident(
            ...     customer_id="gh_001",
            ...     embedding=np.random.rand(8),
            ...     anomaly_score=0.75,
            ...     sequence=sensor_seq,
            ...     embedding_uncertainty=0.12,
            ...     root_cause="boiler_failure",
            ...     tags=["recurring", "urgent"],
            ...     notes="Third occurrence this week"
            ... )
            >>> incident_id
            'a1b2c3d4-...'
        """
        
        incident = Incident(
            customer_id=customer_id,
            embedding=embedding.tolist(),  # pgvector handles conversion
            anomaly_score=float(anomaly_score),
            sequence=sequence.tolist() if isinstance(sequence, np.ndarray) else sequence,
            embedding_uncertainty=float(embedding_uncertainty),
            root_cause=root_cause,
            detected_at=datetime.utcnow(),
            **metadata
        )
        
        self.db.add(incident)
        self.db.commit()
        self.db.refresh(incident)
        
        logger.info(f"Stored incident {incident.id} for customer {customer_id}")
        return str(incident.id)
    
    # =========================================================================
    # FIND SIMILAR INCIDENTS
    # =========================================================================
    
    def find_similar(self,
                    embedding: np.ndarray,
                    customer_id: Optional[str] = None,
                    k: int = 5,
                    max_age_days: Optional[int] = None,
                    resolved_only: bool = False) -> List[Incident]:
        """
        Find k most similar past incidents using cosine distance in embedding space.
        
        Args:
            embedding: 8-dimensional query vector
            customer_id: Filter to specific customer (None = all customers)
            k: Number of neighbors to return
            max_age_days: Only return incidents from last N days (None = all)
            resolved_only: Only return resolved incidents
        
        Returns:
            List of Incident objects, ordered by similarity (most similar first)
        
        Example:
            >>> new_embedding = np.array([...])  # 8-dim
            >>> similar = memory.find_similar(
            ...     embedding=new_embedding,
            ...     customer_id="gh_001",
            ...     k=5,
            ...     resolved_only=True
            ... )
            >>> for incident in similar:
            ...     print(f"Similarity: {incident.embedding_distance}")
            ...     print(f"Root cause: {incident.root_cause}")
            ...     print(f"Resolved: {incident.resolved_at}")
        """
        
        # Convert numpy array to list for pgvector
        embedding_list = embedding.tolist()
        
        # Build query with filters
        query = self.db.query(
            Incident,
            # Cosine distance: <-> operator
            (Incident.embedding.cosine_distance(embedding_list)).label("distance")
        )
        
        # Filter by customer
        if customer_id:
            query = query.filter(Incident.customer_id == customer_id)
        
        # Filter by age
        if max_age_days:
            cutoff = datetime.utcnow().timestamp() - (max_age_days * 86400)
            query = query.filter(Incident.detected_at >= cutoff)
        
        # Filter by resolution status
        if resolved_only:
            query = query.filter(Incident.resolved == True)
        
        # Order by distance and limit
        results = query.order_by("distance").limit(k).all()
        
        return [incident for incident, distance in results]
    
    def find_similar_by_root_cause(self,
                                   embedding: np.ndarray,
                                   customer_id: Optional[str] = None,
                                   k: int = 5) -> Dict[str, List[Incident]]:
        """
        Find similar incidents and group by root cause.
        
        Returns:
            Dict mapping root_cause → List[Incident]
        
        Example:
            >>> grouped = memory.find_similar_by_root_cause(embedding, k=10)
            >>> grouped['boiler_failure']  # All similar boiler failures
            [<Incident ...>, <Incident ...>, ...]
        """
        similar = self.find_similar(embedding, customer_id=customer_id, k=k)
        
        grouped = {}
        for incident in similar:
            cause = incident.root_cause or "unknown"
            if cause not in grouped:
                grouped[cause] = []
            grouped[cause].append(incident)
        
        return grouped
    
    # =========================================================================
    # SUGGEST ROOT CAUSES & FIXES
    # =========================================================================
    
    def suggest_fixes(self,
                     embedding: np.ndarray,
                     customer_id: Optional[str] = None,
                     k: int = 10,
                     min_confidence: float = 0.5) -> Dict:
        """
        Suggest root cause and actions based on similar past incidents.
        
        Returns:
            Dict with:
            - most_likely_cause: (root_cause, confidence, count)
            - all_causes: [(cause, confidence, count), ...]
            - recommended_actions: [(action, effectiveness_rate), ...]
            - similar_incidents: [Incident, ...]
        
        Example:
            >>> suggestions = memory.suggest_fixes(embedding, customer_id="gh_001")
            >>> print(suggestions['most_likely_cause'])
            ('boiler_failure', 0.85, 3)  # 85% confidence, seen 3 times
            
            >>> print(suggestions['recommended_actions'])
            [('restart_heating', 0.9), ('check_pressure', 0.8), ...]
        """
        
        # Find similar incidents
        similar = self.find_similar(
            embedding=embedding,
            customer_id=customer_id,
            k=k,
            resolved_only=True  # Only learn from resolved cases
        )
        
        if not similar:
            return {
                "most_likely_cause": None,
                "all_causes": [],
                "recommended_actions": [],
                "similar_incidents": [],
                "warning": "No similar resolved incidents found"
            }
        
        # Analyze root causes
        cause_counts = {}
        for incident in similar:
            if incident.root_cause:
                cause_counts[incident.root_cause] = cause_counts.get(incident.root_cause, 0) + 1
        
        # Rank causes by frequency
        ranked_causes = sorted(cause_counts.items(), key=lambda x: x[1], reverse=True)
        
        # Analyze recommended actions
        action_stats = {}  # action → (count, effective_count)
        for incident in similar:
            if incident.actions_taken:
                for action in incident.actions_taken:
                    if action not in action_stats:
                        action_stats[action] = [0, 0]
                    action_stats[action][0] += 1
                    if incident.actions_effective:
                        action_stats[action][1] += 1
        
        # Compute effectiveness rates
        recommended_actions = []
        for action, (total, effective) in action_stats.items():
            effectiveness = effective / total if total > 0 else 0
            if effectiveness >= min_confidence:
                recommended_actions.append((action, effectiveness, total))
        
        recommended_actions.sort(key=lambda x: x[1], reverse=True)  # Sort by effectiveness
        
        return {
            "most_likely_cause": (
                ranked_causes[0][0],
                ranked_causes[0][1] / len(similar),  # Confidence as frequency
                ranked_causes[0][1]  # Count
            ) if ranked_causes else None,
            "all_causes": [
                (cause, count / len(similar), count)
                for cause, count in ranked_causes
            ],
            "recommended_actions": recommended_actions,
            "similar_incidents": similar,
            "confidence": len(similar)  # How many similar cases to base decision on
        }
    
    # =========================================================================
    # UPDATE INCIDENTS
    # =========================================================================
    
    def resolve_incident(self,
                        incident_id: str,
                        root_cause: str,
                        actions_taken: List[str],
                        actions_effective: bool,
                        notes: Optional[str] = None) -> None:
        """
        Mark incident as resolved with root cause and actions taken.
        
        Args:
            incident_id: UUID of incident
            root_cause: Determined root cause (e.g., "boiler_failure")
            actions_taken: List of actions (e.g., ["restart_heating", "increase_fan"])
            actions_effective: Did the actions fix the problem?
            notes: Operator notes
        
        Example:
            >>> memory.resolve_incident(
            ...     incident_id="a1b2c3d4-...",
            ...     root_cause="boiler_failure",
            ...     actions_taken=["restart_heating_system"],
            ...     actions_effective=True,
            ...     notes="Boiler pressure reset, system running normally"
            ... )
        """
        
        incident = self.db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            raise ValueError(f"Incident {incident_id} not found")
        
        incident.root_cause = root_cause
        incident.actions_taken = actions_taken
        incident.actions_effective = actions_effective
        incident.resolved = True
        incident.resolved_at = datetime.utcnow()
        incident.notes = notes
        
        # Compute time to resolve
        if incident.detected_at:
            time_diff = incident.resolved_at - incident.detected_at
            incident.time_to_resolve = int(time_diff.total_seconds() / 60)  # Minutes
        
        self.db.commit()
        logger.info(f"Resolved incident {incident_id} with cause={root_cause}")
    
    def mark_anomaly(self,
                    incident_id: str,
                    is_anomaly: bool) -> None:
        """
        Operator feedback: "This was/wasn't really an anomaly."
        
        Used for active learning and threshold tuning.
        """
        incident = self.db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            raise ValueError(f"Incident {incident_id} not found")
        
        incident.tags = incident.tags or []
        if is_anomaly and "confirmed_anomaly" not in incident.tags:
            incident.tags.append("confirmed_anomaly")
        elif not is_anomaly and "false_positive" not in incident.tags:
            incident.tags.append("false_positive")
        
        self.db.commit()
        logger.info(f"Marked incident {incident_id} as anomaly={is_anomaly}")
    
    # =========================================================================
    # ANALYTICS & REPORTING
    # =========================================================================
    
    def get_statistics(self, customer_id: Optional[str] = None) -> Dict:
        """
        Compute incident statistics.
        
        Returns:
            Dict with:
            - total_incidents: int
            - resolved_rate: float (0-1)
            - avg_time_to_resolve: float (minutes)
            - top_root_causes: [(cause, count), ...]
        """
        
        query = self.db.query(Incident)
        if customer_id:
            query = query.filter(Incident.customer_id == customer_id)
        
        total = query.count()
        resolved = query.filter(Incident.resolved == True).count()
        
        # Average resolution time
        avg_time = self.db.query(func.avg(Incident.time_to_resolve)).filter(
            Incident.resolved == True
        ).scalar()
        
        # Top root causes
        top_causes = self.db.query(
            Incident.root_cause,
            func.count(Incident.id).label("count")
        ).filter(
            Incident.root_cause.isnot(None)
        ).group_by(Incident.root_cause).order_by(desc("count")).limit(10).all()
        
        return {
            "total_incidents": total,
            "resolved_incidents": resolved,
            "resolved_rate": resolved / total if total > 0 else 0,
            "avg_time_to_resolve_minutes": float(avg_time) if avg_time else None,
            "top_root_causes": [(cause, count) for cause, count in top_causes]
        }
    
    def get_recurring_issues(self,
                            customer_id: Optional[str] = None,
                            min_occurrences: int = 3) -> List[Dict]:
        """
        Find recurring issues (same root cause multiple times).
        
        Returns:
            List of {root_cause, count, last_occurrence, avg_time_to_resolve}
        """
        
        query = self.db.query(Incident)
        if customer_id:
            query = query.filter(Incident.customer_id == customer_id)
        
        recurring = self.db.query(
            Incident.root_cause,
            func.count(Incident.id).label("count"),
            func.max(Incident.detected_at).label("last_occurrence"),
            func.avg(Incident.time_to_resolve).label("avg_resolve_time")
        ).filter(
            Incident.root_cause.isnot(None)
        ).group_by(Incident.root_cause).having(
            func.count(Incident.id) >= min_occurrences
        ).order_by(desc("count")).all()
        
        return [
            {
                "root_cause": cause,
                "occurrences": count,
                "last_occurrence": last_occ,
                "avg_time_to_resolve_minutes": avg_time
            }
            for cause, count, last_occ, avg_time in recurring
        ]
```

---

### STEP 4: Create API Routes

`src/api/routes/incidents.py`:

```python
"""
API endpoints for incident memory system.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
import numpy as np
from datetime import datetime
from typing import List, Optional

from src.db.session import get_db
from src.db.incident_memory import IncidentMemory
from src.api.schemas.incidents import (
    IncidentCreateRequest,
    IncidentResponse,
    SuggestionsResponse,
    StatisticsResponse
)

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.post("/store")
async def store_incident(
    request: IncidentCreateRequest,
    db: Session = Depends(get_db)
) -> dict:
    """
    Store a detected anomaly as an incident.
    """
    memory = IncidentMemory(db)
    
    incident_id = memory.store_incident(
        customer_id=request.customer_id,
        embedding=np.array(request.embedding),
        anomaly_score=request.anomaly_score,
        sequence=np.array(request.sequence),
        embedding_uncertainty=request.embedding_uncertainty,
        root_cause=request.root_cause,
        tags=request.tags,
        notes=request.notes
    )
    
    return {"incident_id": incident_id, "status": "stored"}


@router.get("/similar")
async def find_similar(
    embedding: List[float] = Query(...),
    customer_id: Optional[str] = Query(None),
    k: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db)
) -> List[IncidentResponse]:
    """
    Find k most similar past incidents.
    """
    memory = IncidentMemory(db)
    
    similar = memory.find_similar(
        embedding=np.array(embedding),
        customer_id=customer_id,
        k=k
    )
    
    return [IncidentResponse.from_orm(incident) for incident in similar]


@router.get("/suggest-fixes")
async def suggest_fixes(
    embedding: List[float] = Query(...),
    customer_id: Optional[str] = Query(None),
    db: Session = Depends(get_db)
) -> SuggestionsResponse:
    """
    Get suggested root cause and fixes based on history.
    """
    memory = IncidentMemory(db)
    
    suggestions = memory.suggest_fixes(
        embedding=np.array(embedding),
        customer_id=customer_id
    )
    
    return SuggestionsResponse(**suggestions)


@router.post("/resolve/{incident_id}")
async def resolve_incident(
    incident_id: str,
    root_cause: str,
    actions_taken: List[str],
    actions_effective: bool,
    notes: Optional[str] = None,
    db: Session = Depends(get_db)
) -> dict:
    """
    Mark incident as resolved with metadata.
    """
    memory = IncidentMemory(db)
    
    try:
        memory.resolve_incident(
            incident_id=incident_id,
            root_cause=root_cause,
            actions_taken=actions_taken,
            actions_effective=actions_effective,
            notes=notes
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    return {"status": "resolved"}


@router.get("/statistics")
async def get_statistics(
    customer_id: Optional[str] = Query(None),
    db: Session = Depends(get_db)
) -> StatisticsResponse:
    """
    Get incident statistics.
    """
    memory = IncidentMemory(db)
    stats = memory.get_statistics(customer_id=customer_id)
    return StatisticsResponse(**stats)


@router.get("/recurring-issues")
async def get_recurring_issues(
    customer_id: Optional[str] = Query(None),
    min_occurrences: int = Query(3, ge=1),
    db: Session = Depends(get_db)
) -> List[dict]:
    """
    Find recurring issues.
    """
    memory = IncidentMemory(db)
    return memory.get_recurring_issues(
        customer_id=customer_id,
        min_occurrences=min_occurrences
    )
```

---

### STEP 5: Create Pydantic Schemas

`src/api/schemas/incidents.py`:

```python
"""
Schemas for incident API endpoints.
"""

from pydantic import BaseModel
from typing import List, Optional, Dict, Tuple
from datetime import datetime


class IncidentCreateRequest(BaseModel):
    """Request to store new incident."""
    customer_id: str
    embedding: List[float]  # 8-dim
    anomaly_score: float
    sequence: List[List[float]]  # (seq_len, 4)
    embedding_uncertainty: float
    root_cause: Optional[str] = None
    tags: Optional[List[str]] = []
    notes: Optional[str] = None


class IncidentResponse(BaseModel):
    """Single incident response."""
    id: str
    customer_id: str
    detected_at: datetime
    anomaly_score: float
    root_cause: Optional[str]
    root_cause_confidence: Optional[float]
    resolved: bool
    resolved_at: Optional[datetime]
    actions_taken: Optional[List[str]]
    actions_effective: Optional[bool]
    time_to_resolve: Optional[int]
    
    class Config:
        from_attributes = True


class SuggestionsResponse(BaseModel):
    """Suggested root cause and fixes."""
    most_likely_cause: Optional[Tuple[str, float, int]] = None  # (cause, confidence, count)
    all_causes: List[Tuple[str, float, int]]
    recommended_actions: List[Tuple[str, float, int]]  # (action, effectiveness, count)
    similar_incidents: List[IncidentResponse]
    confidence: int  # Number of similar cases used


class StatisticsResponse(BaseModel):
    """Incident statistics."""
    total_incidents: int
    resolved_incidents: int
    resolved_rate: float
    avg_time_to_resolve_minutes: Optional[float]
    top_root_causes: List[Tuple[str, int]]
```

---

### STEP 6: Create Tests

`tests/db/test_incident_memory.py`:

```python
import pytest
import numpy as np
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Incident
from src.db.incident_memory import IncidentMemory


@pytest.fixture
def test_db():
    """Create in-memory SQLite test database."""
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture
def memory(test_db):
    return IncidentMemory(test_db)


class TestIncidentMemory:
    
    def test_store_incident(self, memory):
        """Should store incident with embedding."""
        incident_id = memory.store_incident(
            customer_id="gh_001",
            embedding=np.random.rand(8),
            anomaly_score=0.75,
            sequence=np.random.rand(20, 4),
            embedding_uncertainty=0.12
        )
        
        assert incident_id is not None
        assert len(incident_id) > 0
    
    def test_find_similar(self, memory):
        """Should find similar incidents."""
        # Store reference embedding
        ref_emb = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
        memory.store_incident(
            customer_id="gh_001",
            embedding=ref_emb,
            anomaly_score=0.8,
            sequence=np.random.rand(20, 4),
            embedding_uncertainty=0.1,
            root_cause="boiler_failure"
        )
        
        # Store similar embedding
        similar_emb = ref_emb + np.random.randn(8) * 0.01
        memory.store_incident(
            customer_id="gh_001",
            embedding=similar_emb,
            anomaly_score=0.75,
            sequence=np.random.rand(20, 4),
            embedding_uncertainty=0.1,
            root_cause="boiler_failure"
        )
        
        # Find similar
        results = memory.find_similar(embedding=ref_emb, k=1)
        assert len(results) > 0
    
    def test_suggest_fixes(self, memory):
        """Should suggest fixes based on history."""
        # Store resolved incidents
        for i in range(3):
            memory.store_incident(
                customer_id="gh_001",
                embedding=np.random.rand(8),
                anomaly_score=0.8,
                sequence=np.random.rand(20, 4),
                embedding_uncertainty=0.1,
                root_cause="boiler_failure"
            )
        
        # Resolve them
        for i in range(3):
            incidents = memory.db.query(Incident).all()
            memory.resolve_incident(
                incident_id=str(incidents[i].id),
                root_cause="boiler_failure",
                actions_taken=["restart_heating"],
                actions_effective=True
            )
        
        # Get suggestions
        suggestions = memory.suggest_fixes(
            embedding=np.random.rand(8),
            customer_id="gh_001"
        )
        
        assert suggestions["most_likely_cause"] is not None
        assert suggestions["recommended_actions"] is not None
    
    def test_statistics(self, memory):
        """Should compute statistics."""
        # Store and resolve incidents
        incident_id = memory.store_incident(
            customer_id="gh_001",
            embedding=np.random.rand(8),
            anomaly_score=0.8,
            sequence=np.random.rand(20, 4),
            embedding_uncertainty=0.1
        )
        
        memory.resolve_incident(
            incident_id=incident_id,
            root_cause="power_glitch",
            actions_taken=["restart_system"],
            actions_effective=True
        )
        
        stats = memory.get_statistics(customer_id="gh_001")
        
        assert stats["total_incidents"] == 1
        assert stats["resolved_incidents"] == 1
        assert stats["resolved_rate"] == 1.0
```

---

## 📊 INTEGRATION CHECKLIST

- [ ] Update `src/db/models.py` with Incident + IncidentAction tables
- [ ] Create migration `003_add_incident_memory_tables.py`
- [ ] Run migration: `alembic upgrade head`
- [ ] Create `src/db/incident_memory.py`
- [ ] Create `src/api/routes/incidents.py`
- [ ] Create `src/api/schemas/incidents.py`
- [ ] Create tests in `tests/db/test_incident_memory.py`
- [ ] Create tests in `tests/api/test_incident_api.py`
- [ ] Update main API router to include incidents routes
- [ ] Update dashboard to show incident history + suggestions

---

## 🧪 TESTING STRATEGY

### Unit Tests
```bash
pytest tests/db/test_incident_memory.py -v
pytest tests/api/test_incident_api.py -v
```

### Integration Tests
```bash
# Test end-to-end: detect anomaly → store → find similar → suggest fixes
pytest tests/integration/test_incident_workflow.py -v
```

### Manual Validation
1. Store 50+ anomalies with embeddings
2. Query for similar ones
3. Verify cosine distances make sense
4. Test "suggest fixes" with known root causes

---

## 🚀 DEPLOYMENT

### Phase:
- **Phase 1.5 Week 1, Day 1.5-2**
- Before: Vector embeddings working
- After: Online learning module

### PostgreSQL Setup:
```bash
# Production PostgreSQL with pgvector:
docker run --name pg-vector \
  -e POSTGRES_PASSWORD=secure_password \
  -e POSTGRES_DB=ai_agronomist_prod \
  -p 5432:5432 \
  pgvector/pgvector:latest
```

### Performance Tuning:
- IVFFLAT index automatically created for vector search
- Expected query time: <100ms for k=5 across 10K incidents
- If slower, tune IVFFLAT parameters or switch to HNSW

---

## 💾 DEPENDENCIES

Add to `requirements.txt`:
```
pgvector>=0.2.0
psycopg2-binary>=2.9.0
sqlalchemy>=2.0.0
alembic>=1.11.0
```

---

## 📈 SUCCESS CRITERIA

- [ ] Store 100+ incidents with embeddings
- [ ] Similar incidents cluster (cosine distance < 0.3)
- [ ] "Suggest fixes" works: shows root cause + actions for new anomaly
- [ ] Vector search completes in <100ms
- [ ] API returns structured suggestions
- [ ] Dashboard displays incident history

---

## 🎯 NEXT STEPS

→ Proceed to **Gap 3: Online Learning Pipeline** once incident memory is working.

**Expected Time:** 2 days  
**Output:** Production incident memory with semantic search  
**Foundation for:** Automated anomaly classification, operator suggestions, MTTR reduction
