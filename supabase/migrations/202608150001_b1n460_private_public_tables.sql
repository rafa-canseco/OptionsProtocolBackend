-- B1N-460: Supabase's public schema is API-exposed by default.
--
-- The application accesses these tables only through the backend with the
-- service_role key. Keep service_role privileges intact while denying direct
-- table and sequence access to browser-facing roles.

-- Fail atomically instead of waiting behind application traffic indefinitely.
SET lock_timeout = '5s';

-- Supabase grants API roles access to new public relations by default. Override
-- those defaults before inspecting current relations. PostgreSQL cannot enable
-- RLS through default privileges, so CI separately requires every future
-- CREATE TABLE migration to enable RLS explicitly.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL PRIVILEGES ON SEQUENCES FROM anon, authenticated;

DO $migration$
DECLARE
  relation RECORD;
BEGIN
  FOR relation IN
    SELECT ns.nspname AS schema_name, cls.relname AS relation_name
    FROM pg_class AS cls
    JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
    WHERE ns.nspname = 'public'
      AND cls.relkind IN ('r', 'p')
    ORDER BY cls.oid
  LOOP
    EXECUTE format(
      'ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY',
      relation.schema_name,
      relation.relation_name
    );
    EXECUTE format(
      'REVOKE ALL PRIVILEGES ON TABLE %I.%I FROM anon, authenticated',
      relation.schema_name,
      relation.relation_name
    );
  END LOOP;
END
$migration$;

-- Revoke API-role privileges from all relations after the catalog loop. The
-- rollout must prohibit concurrent DDL and finish with an RLS catalog assertion.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;

RESET lock_timeout;
