-- Run as the Ketoshop database owner after the read-only role exists.
-- This file provisions views only; create the role and password separately.
GRANT USAGE ON SCHEMA public TO ketoshop_diagnostics;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ketoshop_diagnostics;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ketoshop_diagnostics;

-- Safe casts keep malformed legacy JSON/number text from making a view fail.
-- These helpers read no tables and only attempt safe casts. The diagnostics
-- login needs EXECUTE because PostgreSQL checks function privileges when
-- evaluating view expressions; they do not provide raw table access.
CREATE OR REPLACE FUNCTION public.ketoshop_try_jsonb(value text)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE
AS $$
BEGIN
    RETURN value::jsonb;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION public.ketoshop_try_numeric(value text)
RETURNS numeric
LANGUAGE plpgsql IMMUTABLE
AS $$
BEGIN
    RETURN value::numeric;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION public.ketoshop_try_integer(value text)
RETURNS integer
LANGUAGE plpgsql IMMUTABLE
AS $$
BEGIN
    RETURN value::integer;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION public.ketoshop_try_jsonb(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.ketoshop_try_numeric(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.ketoshop_try_integer(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.ketoshop_try_jsonb(text) TO ketoshop_diagnostics;
GRANT EXECUTE ON FUNCTION public.ketoshop_try_numeric(text) TO ketoshop_diagnostics;
GRANT EXECUTE ON FUNCTION public.ketoshop_try_integer(text) TO ketoshop_diagnostics;

CREATE OR REPLACE VIEW public.ketoshop_diag_orders
WITH (security_barrier = true) AS
WITH normalized AS (
    SELECT
        id,
        created_at,
        status,
        ROUND(total::numeric, 2) AS total,
        CASE
            WHEN public.ketoshop_try_jsonb(items) IS NOT NULL
                THEN public.ketoshop_try_jsonb(items)
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
                WHEN public.ketoshop_try_numeric(item ->> 'quantity') IS NOT NULL
                    THEN public.ketoshop_try_numeric(item ->> 'quantity')
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
            WHEN public.ketoshop_try_jsonb(items) IS NOT NULL
                THEN public.ketoshop_try_jsonb(items)
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
        WHEN public.ketoshop_try_numeric(item.value ->> 'quantity') IS NOT NULL
            THEN public.ketoshop_try_numeric(item.value ->> 'quantity')
        ELSE NULL
    END AS quantity,
    LEFT(COALESCE(item.value ->> 'unit', ''), 32) AS unit,
    CASE
        WHEN public.ketoshop_try_numeric(item.value ->> 'quantity') IS NOT NULL
         AND public.ketoshop_try_numeric(item.value ->> 'price') IS NOT NULL
            THEN ROUND(
                public.ketoshop_try_numeric(item.value ->> 'quantity')
                * public.ketoshop_try_numeric(item.value ->> 'price'),
                2
            )
        ELSE NULL
    END AS line_amount
FROM normalized
CROSS JOIN LATERAL jsonb_array_elements(
    CASE
        WHEN jsonb_typeof(normalized.parsed_items) = 'array' THEN normalized.parsed_items
        ELSE '[]'::jsonb
    END
) AS item(value);

-- This view estimates item cost from the current catalog. The shop does not
-- snapshot cost_price in historical order JSON, so every row states that
-- historical cost is unavailable and counts lines whose current cost is
-- missing instead of treating them as zero.
CREATE OR REPLACE VIEW public.ketoshop_diag_finance_orders
WITH (security_barrier = true) AS
WITH normalized AS (
    SELECT
        id,
        created_at,
        CASE
            WHEN status IN ('pending', 'confirmed', 'shipped', 'delivered', 'cancelled')
                THEN status
            ELSE 'other'
        END AS status,
        CASE
            WHEN COALESCE(source, 'bot') IN ('bot', 'manual', 'b2b')
                THEN COALESCE(source, 'bot')
            ELSE 'other'
        END AS source,
        ROUND(total::numeric, 2) AS amount,
        CASE
            WHEN public.ketoshop_try_jsonb(items) IS NOT NULL
                THEN public.ketoshop_try_jsonb(items)
            ELSE '[]'::jsonb
        END AS parsed_items,
        CASE
            WHEN public.ketoshop_try_jsonb(items) IS NOT NULL
                THEN jsonb_typeof(public.ketoshop_try_jsonb(items)) = 'array'
            ELSE FALSE
        END AS items_are_valid
    FROM public.orders
), current_costs AS (
    SELECT
        o.id,
        COALESCE(SUM(
            CASE
                WHEN line.value IS NOT NULL
                 AND COALESCE(line.value ->> 'is_set', 'false') <> 'true'
                 AND public.ketoshop_try_numeric(line.value ->> 'quantity') > 0
                 AND product.cost_price IS NOT NULL
                 AND product.cost_price > 0
                    THEN ROUND(
                        product.cost_price::numeric
                        * public.ketoshop_try_numeric(line.value ->> 'quantity'),
                        2
                    )
                ELSE 0::numeric
            END
        ), 0::numeric) AS current_catalog_cost,
        COALESCE(SUM(
            CASE
                WHEN line.value IS NULL THEN 0
                WHEN COALESCE(line.value ->> 'is_set', 'false') = 'true' THEN 1
                WHEN public.ketoshop_try_numeric(line.value ->> 'quantity') IS NULL THEN 1
                WHEN public.ketoshop_try_numeric(line.value ->> 'quantity') <= 0 THEN 1
                WHEN product.id IS NULL THEN 1
                WHEN product.cost_price IS NULL OR product.cost_price <= 0 THEN 1
                ELSE 0
            END
        ), 0)::integer AS missing_cost_items
    FROM normalized AS o
    LEFT JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(o.parsed_items) = 'array' THEN o.parsed_items
            ELSE '[]'::jsonb
        END
    ) AS line(value) ON TRUE
    LEFT JOIN public.products AS product
        ON product.id = public.ketoshop_try_integer(
            COALESCE(line.value ->> 'product_id', line.value ->> 'id')
        )
       AND COALESCE(line.value ->> 'is_set', 'false') <> 'true'
    GROUP BY o.id
)
SELECT
    o.id AS order_id,
    o.created_at,
    o.status,
    o.source,
    CASE WHEN o.status = 'delivered' THEN o.amount ELSE 0::numeric END AS revenue,
    CASE
        WHEN o.status <> 'delivered' THEN 0::numeric
        WHEN o.items_are_valid THEN COALESCE(c.current_catalog_cost, 0::numeric)
        ELSE 0::numeric
    END AS current_catalog_cost,
    CASE
        WHEN o.status <> 'delivered' THEN 0
        WHEN o.items_are_valid THEN COALESCE(c.missing_cost_items, 0)
        ELSE 1
    END AS missing_cost_items,
    'current_catalog'::text AS cost_basis,
    FALSE AS historical_cost_data_available
FROM normalized AS o
LEFT JOIN current_costs AS c ON c.id = o.id;

CREATE OR REPLACE VIEW public.ketoshop_diag_expenses
WITH (security_barrier = true) AS
SELECT created_at, ROUND(amount::numeric, 2) AS amount
FROM public.expenses;

REVOKE ALL ON public.ketoshop_diag_orders, public.ketoshop_diag_order_items
    FROM PUBLIC;
GRANT SELECT ON public.ketoshop_diag_orders, public.ketoshop_diag_order_items
    TO ketoshop_diagnostics;
REVOKE ALL ON public.ketoshop_diag_finance_orders, public.ketoshop_diag_expenses
    FROM PUBLIC;
GRANT SELECT ON public.ketoshop_diag_finance_orders, public.ketoshop_diag_expenses
    TO ketoshop_diagnostics;
