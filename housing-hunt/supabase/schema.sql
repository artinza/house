-- One generic table. path looks like "votes/L123__artin". coll is the collection name.
create table if not exists docs (
  path text primary key,
  coll text not null check (coll in ('votes','comments','meta','prefs','shared')),
  data jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);
create index if not exists docs_coll on docs(coll);
alter table docs enable row level security;
-- Anyone holding the anon key can read and write. Fine for two friends, do not store anything private.
create policy "read" on docs for select using (true);
create policy "insert" on docs for insert with check (true);
create policy "update" on docs for update using (true) with check (true);
create policy "delete" on docs for delete using (coll in ('votes','comments'));
