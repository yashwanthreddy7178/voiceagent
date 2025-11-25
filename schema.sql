-- Enable UUID extension if not already enabled
create extension if not exists "uuid-ossp";

-- 1. Organizations (Stores)
create table public.organizations (
  id uuid default uuid_generate_v4() primary key,
  name text not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 2. Agent Configurations (Per Store settings)
create table public.agent_configs (
  id uuid default uuid_generate_v4() primary key,
  org_id uuid references public.organizations(id) on delete cascade not null,
  system_prompt_template text not null,
  voice_model text default 'aura-asteria-en',
  greeting text,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 3. Phone Numbers (Mapping Twilio numbers to Stores)
create table public.phone_numbers (
  id uuid default uuid_generate_v4() primary key,
  org_id uuid references public.organizations(id) on delete cascade not null,
  phone_number text not null unique, -- E.164 format e.g. +15551234567
  sid text, -- Twilio Phone Number SID
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 4. Update Calls table to link to Organizations
alter table public.calls 
add column org_id uuid references public.organizations(id);

-- Enable Row Level Security (RLS) - Optional for now but good practice
alter table public.organizations enable row level security;
alter table public.agent_configs enable row level security;
alter table public.phone_numbers enable row level security;

-- Create policies (Open for now for server access, lock down later)
create policy "Enable read access for all users" on public.organizations for select using (true);
create policy "Enable read access for all users" on public.agent_configs for select using (true);
create policy "Enable read access for all users" on public.phone_numbers for select using (true);
