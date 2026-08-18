-- B1N-455: normalize legacy TEXT premium fields before the MM exposure RPC
-- performs numeric aggregation. The explicit casts also remain valid when a
-- column is already NUMERIC.

ALTER TABLE public.order_events
    ALTER COLUMN gross_premium TYPE NUMERIC
        USING (gross_premium::NUMERIC),
    ALTER COLUMN net_premium TYPE NUMERIC
        USING (net_premium::NUMERIC),
    ALTER COLUMN protocol_fee TYPE NUMERIC
        USING (protocol_fee::NUMERIC);
