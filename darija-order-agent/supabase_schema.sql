-- OPTIONAL. Run in Supabase -> SQL Editor. Orders are mirrored here from SQLite.
create table if not exists public.orders (
  id text primary key,
  customer_name text not null,
  phone text not null,
  product_name text not null,
  quantity integer not null default 1,
  total_dzd integer not null,
  delivery_address text not null,
  status text not null default 'PENDING_CONFIRMATION',
  confirmed_at timestamptz,
  created_at timestamptz not null default now()
);
-- Row Level Security on: only the service_role key (used by this server) can read/write.
alter table public.orders enable row level security;
