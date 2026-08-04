-- The production definition is NOT NULL. The migration deliberately uses
-- `IS NOT TRUE`, so relax only this ephemeral fixture to prove compatibility
-- with a historical/null row that could exist before that constraint.
ALTER TABLE public.order_events
    ALTER COLUMN is_settled DROP NOT NULL;

GRANT SELECT, INSERT, DELETE ON public.mm_quotes TO service_role;
GRANT SELECT, INSERT, DELETE ON public.order_events TO service_role;
