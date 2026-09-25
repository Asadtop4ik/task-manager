-- Run as the Ketoshop database owner after the read-only role exists.
-- This file provisions views only; create the role and password separately.
GRANT USAGE ON SCHEMA public TO ketoshop_diagnostics;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ketoshop_diagnostics;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ketoshop_diagnostics;

CREATE OR REPLACE VIEW public.ketoshop_diag_orders
WITH (security_barrier = true) AS
WITH normalized AS (
    SELECT
        id,
        created_at,
        status,
        total,
        CASE
            WHEN pg_input_is_valid(items, 'jsonb') THEN items::jsonb
            ELSE '[]'::jsonb
        END AS parsed_items
    FROM public.orders
)
SELECT
    id AS order_id,
    created_at,
    status,
    total,
    COALESCE((
        SELECT SUM(
            CASE
                WHEN COALESCE(item ->> 'quantity', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
                    THEN (item ->> 'quantity')::double precision
                ELSE 0
            END
        )
        FROM jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(parsed_items) = 'array' THEN parsed_items
                ELSE '[]'::jsonb
            END
        ) AS item
    ), 0) AS quantity_total
FROM normalized;

CREATE OR REPLACE VIEW public.ketoshop_diag_order_items
WITH (security_barrier = true) AS
WITH normalized AS (
    SELECT
        id,
        created_at,
        status,
        CASE
            WHEN pg_input_is_valid(items, 'jsonb') THEN items::jsonb
            ELSE '[]'::jsonb
        END AS parsed_items
    FROM public.orders
)
SELECT
    normalized.id AS order_id,
    normalized.created_at,
    normalized.status,
    LEFT(COALESCE(item.value ->> 'name', ''), 160) AS item_name,
    CASE
        WHEN COALESCE(item.value ->> 'quantity', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
            THEN (item.value ->> 'quantity')::double precision
        ELSE 0
    END AS quantity,
    LEFT(COALESCE(item.value ->> 'unit', ''), 32) AS unit,
    CASE
        WHEN COALESCE(item.value ->> 'quantity', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
         AND COALESCE(item.value ->> 'price', '') ~ '^-?[0-9]+(\.[0-9]+)?$'
            THEN (item.value ->> 'quantity')::double precision
               * (item.value ->> 'price')::double precision
        ELSE 0
    END AS line_amount
FROM normalized
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(normalized.parsed_items) = 'array' THEN normalized.parsed_items
        ELSE '[]'::jsonb
    END
) AS item(value);

REVOKE ALL ON public.ketoshop_diag_orders, public.ketoshop_diag_order_items
    FROM PUBLIC;
GRANT SELECT ON public.ketoshop_diag_orders, public.ketoshop_diag_order_items
    TO ketoshop_diagnostics;
