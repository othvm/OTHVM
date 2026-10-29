-- ============================================================
-- OTHVM — Supabase Database Schema
-- Phase 1: All 6 memory tables + indexes
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor)
-- ============================================================

-- ============================================================
-- Table 1: core_context
-- Fixed context loaded on every Gemini call
-- ============================================================
CREATE TABLE IF NOT EXISTS core_context (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    founder_name TEXT NOT NULL DEFAULT '',
    founder_summary TEXT NOT NULL DEFAULT '',
    othvm_definition TEXT NOT NULL DEFAULT '',
    othvm_capabilities TEXT NOT NULL DEFAULT '',
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enable RLS
ALTER TABLE core_context ENABLE ROW LEVEL SECURITY;

-- Allow service_role full access (n8n uses service_role key)
CREATE POLICY "service_role_full_access_core_context" ON core_context
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- ============================================================
-- Table 2: othman_memory
-- Personal details about the founder
-- Categories: personal_info, preferences, daily_patterns, personal_context
-- ============================================================
CREATE TABLE IF NOT EXISTS othman_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category TEXT NOT NULL CHECK (category IN ('personal_info', 'preferences', 'daily_patterns', 'personal_context')),
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (category, key)
);

ALTER TABLE othman_memory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_othman_memory" ON othman_memory
    FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_othman_memory_category ON othman_memory (category);

-- ============================================================
-- Table 3: othvm_memory
-- System capabilities and operational rules
-- Categories: capabilities, workflows, connected_tools, operational_rules
-- ============================================================
CREATE TABLE IF NOT EXISTS othvm_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category TEXT NOT NULL CHECK (category IN ('capabilities', 'workflows', 'connected_tools', 'operational_rules')),
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (category, key)
);

ALTER TABLE othvm_memory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_othvm_memory" ON othvm_memory
    FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_othvm_memory_category ON othvm_memory (category);

-- ============================================================
-- Table 4: project_memory
-- Context for each active project
-- ============================================================
CREATE TABLE IF NOT EXISTS project_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    description TEXT NOT NULL DEFAULT '',
    last_update TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_steps TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);

ALTER TABLE project_memory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_project_memory" ON project_memory
    FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_project_memory_status ON project_memory (status);

-- ============================================================
-- Table 5: goals_memory
-- Active goals tracking
-- ============================================================
CREATE TABLE IF NOT EXISTS goals_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal_description TEXT NOT NULL,
    deadline TIMESTAMPTZ,
    last_progress TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'in_progress', 'completed', 'abandoned')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE goals_memory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_goals_memory" ON goals_memory
    FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_goals_memory_status ON goals_memory (status);

-- ============================================================
-- Table 6: interaction_log
-- Manual log entries (only when founder explicitly asks)
-- ============================================================
CREATE TABLE IF NOT EXISTS interaction_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE interaction_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_interaction_log" ON interaction_log
    FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_interaction_log_logged_at ON interaction_log (logged_at DESC);

-- ============================================================
-- Table 7: sent_reminders_log (for Calendar Check dedup)
-- Tracks which calendar event reminders have been sent
-- ============================================================
CREATE TABLE IF NOT EXISTS sent_reminders_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id TEXT NOT NULL,
    event_title TEXT NOT NULL DEFAULT '',
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id)
);

ALTER TABLE sent_reminders_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_sent_reminders_log" ON sent_reminders_log
    FOR ALL
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_sent_reminders_event_id ON sent_reminders_log (event_id);
