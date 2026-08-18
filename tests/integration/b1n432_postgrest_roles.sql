CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN;
CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD :'authenticator_password';
GRANT anon, authenticated, service_role TO authenticator;
