-- B1N-460: table privileges alone are insufficient once RLS is enabled.
-- Confirm browser-facing roles cannot bypass policies and the backend role can.

DO $verification$
BEGIN
  IF COALESCE(
    (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'anon'),
    false
  ) THEN
    RAISE EXCEPTION 'anon unexpectedly bypasses RLS';
  END IF;

  IF COALESCE(
    (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'authenticated'),
    false
  ) THEN
    RAISE EXCEPTION 'authenticated unexpectedly bypasses RLS';
  END IF;

  IF NOT COALESCE(
    (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'service_role'),
    false
  ) THEN
    RAISE EXCEPTION 'service_role cannot bypass RLS';
  END IF;
END
$verification$;
