-- Synthetic fixture for a disposable ketoshop_diagnostics_qa database only.
DO $guard$
BEGIN
    IF current_database() <> 'ketoshop_diagnostics_qa' THEN
        RAISE EXCEPTION 'synthetic diagnostics fixture requires database ketoshop_diagnostics_qa';
    END IF;
END
$guard$;

CREATE TABLE public.orders (
    id integer PRIMARY KEY,
    customer_name text,
    phone text,
    address text,
    items text,
    total double precision NOT NULL,
    status text NOT NULL,
    created_at timestamp NOT NULL,
    source text
);

CREATE TABLE public.products (
    id integer PRIMARY KEY,
    cost_price numeric(18, 2)
);
INSERT INTO public.products (id, cost_price) VALUES (1, 25000.00), (2, NULL);

CREATE TABLE public.expenses (
    id integer PRIMARY KEY,
    name text NOT NULL,
    amount double precision NOT NULL,
    created_at timestamp NOT NULL
);

INSERT INTO public.orders (
    id, customer_name, phone, address, items, total, status, created_at, source
)
SELECT
    71000 + n,
    'Synthetic Customer ' || n,
    '+998900000000',
    'Synthetic Test Address ' || n,
    CASE
        WHEN n = 205 THEN '{broken synthetic JSON'
        ELSE json_build_array(json_build_object(
            'product_id', CASE WHEN n % 17 = 0 THEN 2 ELSE 1 END,
            'name', 'Synthetic Keto Product',
            'quantity', 1.25,
            'price', 45000,
            'unit', 'pcs'
        ))::text
    END,
    56250,
    CASE WHEN n % 10 = 0 THEN 'pending' ELSE 'delivered' END,
    (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - ((n % 7) * INTERVAL '1 day'),
    CASE WHEN n % 2 = 0 THEN 'bot' ELSE 'b2b' END
FROM generate_series(1, 205) AS n;

INSERT INTO public.expenses (id, name, amount, created_at)
VALUES
    (1, 'Synthetic delivery supplies', 12000,
     (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '1 day'),
    (2, 'Synthetic packing materials', 8000,
     (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '2 days');

DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ketoshop_diagnostics') THEN
        CREATE ROLE ketoshop_diagnostics NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$role$;
