-- Enable the pgvector extension for incident memory embeddings (Gap 2).
-- Runs automatically on FIRST cluster init only (docker-entrypoint-initdb.d);
-- existing postgres_data volumes must run this statement manually once:
--   docker compose exec postgres psql -U postgres -d greenhouse_monitoring \
--     -c 'CREATE EXTENSION IF NOT EXISTS vector;'
CREATE EXTENSION IF NOT EXISTS vector;
