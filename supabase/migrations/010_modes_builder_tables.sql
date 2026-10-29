create extension if not exists pgcrypto;

create table if not exists public.modes (
  id uuid default gen_random_uuid() primary key,
  name text not null unique,
  description text,
  steps jsonb not null,
  is_active boolean default true,
  created_at timestamptz default now()
);

create table if not exists public.custom_workflows (
  id uuid default gen_random_uuid() primary key,
  name text not null,
  n8n_workflow_id text,
  description text,
  trigger_type text,
  trigger_config jsonb,
  created_at timestamptz default now(),
  is_active boolean default true
);

select table_name, column_name, data_type
from information_schema.columns
where table_schema = 'public' and table_name in ('modes','custom_workflows')
order by table_name, ordinal_position;
