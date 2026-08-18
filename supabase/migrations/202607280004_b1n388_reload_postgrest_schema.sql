-- Refresh PostgREST after replacing the fund ingest function and indexes.
NOTIFY pgrst, 'reload schema';
