-- Auth.js persistence schema for the `web` (BFF) service.
--
-- Target: the existing `ai-agronomist-db` Postgres (`AUTH_DATABASE_URL`),
-- in a dedicated `auth` schema — NOT `public`, which the FastAPI app's own
-- alembic metadata owns. Sharing `public` would make `alembic revision
-- --autogenerate` see these five tables as unknown and emit `DROP TABLE`
-- for them; `sec-p2-api` excludes the `auth` schema from alembic autogen on
-- their side, so the schema name must stay exactly `auth` on both sides.
-- Run once, by hand, against that instance before the first login; there is
-- no migration runner in `web/` today, so this file is the source of truth
-- until one exists. Idempotent — safe to re-run.
--
-- `users` / `accounts` / `sessions` / `verification_token` match the exact
-- table/column names `@auth/pg-adapter` queries (see its `src/index.ts`) —
-- do not rename them; the adapter itself never qualifies them with a schema,
-- so `lib/auth/db.ts` points its Postgres pool's `search_path` at `auth` so
-- the adapter's unqualified queries land here. `user_tenants` is this app's
-- addition: it is what makes "a session resolves to exactly one tenant"
-- true (security phase 2 contract, `docs/security/auth-audit-phase1.md` §7).

create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key default gen_random_uuid(),
  name text,
  email text unique not null,
  "emailVerified" timestamptz,
  image text
);

create table if not exists auth.accounts (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references auth.users (id) on delete cascade,
  type text not null,
  provider text not null,
  "providerAccountId" text not null,
  refresh_token text,
  access_token text,
  expires_at bigint,
  token_type text,
  scope text,
  id_token text,
  session_state text,
  unique (provider, "providerAccountId")
);

create table if not exists auth.sessions (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references auth.users (id) on delete cascade,
  expires timestamptz not null,
  "sessionToken" text unique not null
);

create table if not exists auth.verification_token (
  identifier text not null,
  expires timestamptz not null,
  token text not null,
  primary key (identifier, token)
);

-- One row per operator email, provisioned out-of-band by ops (no self-serve
-- sign-up flow exists). `auth.ts`'s `signIn` callback denies sign-in for any
-- email with no row here — fail closed, not "assign a default tenant".
create table if not exists auth.user_tenants (
  email text primary key,
  tenant_id text not null,
  created_at timestamptz not null default now()
);
