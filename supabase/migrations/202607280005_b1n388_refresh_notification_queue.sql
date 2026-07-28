-- Supabase recovery for a PostgREST schema cache notification queue stall.
SELECT pg_notification_queue_usage();
NOTIFY pgrst, 'reload schema';
