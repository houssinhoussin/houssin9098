-- Create discounts and discount_uses tables (idempotent)
create table if not exists public.discounts (
  id uuid primary key default gen_random_uuid(),
  scope text not null check (scope in ('global','user')),
  user_id bigint,
  percent smallint not null check (percent between 0 and 100),
  active boolean not null default true,
  created_by bigint,
  created_at timestamptz not null default now()
);
create index if not exists discounts_scope_idx on public.discounts(scope);
create index if not exists discounts_user_idx on public.discounts(user_id);
create index if not exists discounts_active_idx on public.discounts(active);

create table if not exists public.discount_uses (
  id bigserial primary key,
  discount_id uuid references public.discounts(id) on delete set null,
  user_id bigint not null,
  purchase_id bigint,
  amount_before int not null,
  amount_after int not null,
  created_at timestamptz not null default now()
);

alter table public.discounts enable row level security;
alter table public.discount_uses enable row level security;

create policy if not exists "service all discounts" on public.discounts
  for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
create policy if not exists "service all discount_uses" on public.discount_uses
  for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
