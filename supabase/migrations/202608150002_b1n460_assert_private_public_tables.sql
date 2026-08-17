-- B1N-460: fail the rollout unless the effective database permissions match
-- the backend-only access model. These checks use effective privileges, so an
-- accidental grant inherited through another role is detected as exposure.

DO $verification$
DECLARE
  violations TEXT;
BEGIN
  SELECT string_agg(format('%I.%I', ns.nspname, cls.relname), ', ' ORDER BY cls.oid)
  INTO violations
  FROM pg_class AS cls
  JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
  WHERE ns.nspname = 'public'
    AND cls.relkind IN ('r', 'p')
    AND NOT cls.relrowsecurity;

  IF violations IS NOT NULL THEN
    RAISE EXCEPTION 'public tables without RLS: %', violations;
  END IF;

  SELECT string_agg(format('%I.%I', ns.nspname, cls.relname), ', ' ORDER BY cls.oid)
  INTO violations
  FROM pg_class AS cls
  JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
  WHERE ns.nspname = 'public'
    AND cls.relkind IN ('r', 'p')
    AND (
      has_table_privilege('anon', cls.oid, 'SELECT')
      OR has_table_privilege('anon', cls.oid, 'INSERT')
      OR has_table_privilege('anon', cls.oid, 'UPDATE')
      OR has_table_privilege('anon', cls.oid, 'DELETE')
      OR has_table_privilege('authenticated', cls.oid, 'SELECT')
      OR has_table_privilege('authenticated', cls.oid, 'INSERT')
      OR has_table_privilege('authenticated', cls.oid, 'UPDATE')
      OR has_table_privilege('authenticated', cls.oid, 'DELETE')
    );

  IF violations IS NOT NULL THEN
    RAISE EXCEPTION 'public tables retain API-role DML privileges: %', violations;
  END IF;

  SELECT string_agg(format('%I.%I', ns.nspname, cls.relname), ', ' ORDER BY cls.oid)
  INTO violations
  FROM pg_class AS cls
  JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
  WHERE ns.nspname = 'public'
    AND cls.relkind IN ('r', 'p')
    AND (
      NOT has_table_privilege('service_role', cls.oid, 'SELECT')
      OR NOT has_table_privilege('service_role', cls.oid, 'INSERT')
      OR NOT has_table_privilege('service_role', cls.oid, 'UPDATE')
      OR NOT has_table_privilege('service_role', cls.oid, 'DELETE')
    );

  IF violations IS NOT NULL THEN
    RAISE EXCEPTION 'service_role lacks required table DML privileges: %', violations;
  END IF;
END
$verification$;
