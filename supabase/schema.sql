-- Run once in the Supabase SQL editor. One row per reader per deep dive.
create table if not exists public.reads (
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  slug text not null,
  progress real not null default 0,
  read_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (user_id, slug)
);

-- Readers can only see and change their own rows.
alter table public.reads enable row level security;

create policy "read own" on public.reads for select using (auth.uid() = user_id);
create policy "insert own" on public.reads for insert with check (auth.uid() = user_id);
create policy "update own" on public.reads for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "delete own" on public.reads for delete using (auth.uid() = user_id);
