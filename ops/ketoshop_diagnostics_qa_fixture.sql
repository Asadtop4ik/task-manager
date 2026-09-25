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
    created_at timestamptz NOT NULL,
    source text
);

INSERT INTO public.orders (id, customer_name, phone, address, items, total, status, created_at, source)
VALUES
    (71001, 'Synthetic Customer One', '+998901234567', 'Synthetic Street 1',
     '[{"name":"Synthetic Keto Bread","quantity":2,"price":45000,"unit":"pcs"}]',
     90000, 'delivered', '2026-09-01T10:00:00Z', 'bot'),
    (71002, 'Synthetic Customer Two', '+998909876543', 'Synthetic Street 2',
     '[{"name":"Synthetic Almond Flour","quantity":1.5,"price":80000,"unit":"kg"}]',
     120000, 'pending', '2026-09-02T11:30:00Z', 'bot');

DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ketoshop_diagnostics') THEN
        CREATE ROLE ketoshop_diagnostics NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$role$;
