-- ============================================================
-- OTHVM — Supabase Database Schema
-- 003: Part A (Dynamic Briefs) + Part B (Streaks) + Part C (Expenses)
-- Run via Management API:  node deploy/_sb_sql.js supabase/003_briefs_streaks_expenses.sql
-- (or paste into Dashboard → SQL Editor)
-- ============================================================

-- ============================================================
-- PART A — Table: briefs_registry
-- Every scheduled brief the agent fully manages (name, schedule,
-- n8n workflow id, run state). The agent creates/updates/pauses them.
-- ============================================================
CREATE TABLE IF NOT EXISTS briefs_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    schedule_cron TEXT NOT NULL DEFAULT '',
    schedule_human TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT true,
    workflow_id TEXT NOT NULL DEFAULT '',
    last_run TIMESTAMPTZ,
    next_run TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE briefs_registry ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_briefs_registry" ON briefs_registry
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_briefs_registry_is_active ON briefs_registry (is_active);

-- ============================================================
-- PART B — Table: streaks
-- Each habit streak. reminder_time = Cairo local HH:MM.
-- reminder_message = custom line shown in the daily Telegram reminder.
-- ============================================================
CREATE TABLE IF NOT EXISTS streaks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    reminder_time TEXT NOT NULL DEFAULT '19:00',
    reminder_message TEXT NOT NULL DEFAULT '',
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_completed DATE,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE streaks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_streaks" ON streaks
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_streaks_is_active ON streaks (is_active);

-- ============================================================
-- PART B — Table: streak_logs
-- One row per streak per day: 'completed' (✅) or 'missed' (❌).
-- UNIQUE(streak_id, log_date) keeps the buttons idempotent.
-- ============================================================
CREATE TABLE IF NOT EXISTS streak_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    streak_id UUID NOT NULL REFERENCES streaks(id) ON DELETE CASCADE,
    log_date DATE NOT NULL DEFAULT ((now() AT TIME ZONE 'Africa/Cairo')::date),
    status TEXT NOT NULL CHECK (status IN ('completed', 'missed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (streak_id, log_date)
);

ALTER TABLE streak_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_streak_logs" ON streak_logs
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_streak_logs_streak_id ON streak_logs (streak_id);
CREATE INDEX idx_streak_logs_log_date ON streak_logs (log_date DESC);

-- ============================================================
-- PART C — Table: expenses
-- Every logged expense. spent_on defaults to Cairo "today".
-- ============================================================
CREATE TABLE IF NOT EXISTS expenses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL DEFAULT 'EGP',
    category TEXT NOT NULL DEFAULT 'متفرقات',
    description TEXT NOT NULL DEFAULT '',
    spent_on DATE NOT NULL DEFAULT ((now() AT TIME ZONE 'Africa/Cairo')::date),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE expenses ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_expenses" ON expenses
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_expenses_spent_on ON expenses (spent_on DESC);
CREATE INDEX idx_expenses_category ON expenses (category);

-- ============================================================
-- PART C — Table: expense_categories
-- Categories the agent uses when logging spending.
-- ============================================================
CREATE TABLE IF NOT EXISTS expense_categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE expense_categories ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_expense_categories" ON expense_categories
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ============================================================
-- PART C — Table: savings_log
-- Money actually put aside (amount + optional note/goal).
-- ============================================================
CREATE TABLE IF NOT EXISTS savings_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL DEFAULT 'EGP',
    note TEXT NOT NULL DEFAULT '',
    saved_on DATE NOT NULL DEFAULT ((now() AT TIME ZONE 'Africa/Cairo')::date),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE savings_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access_savings_log" ON savings_log
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE INDEX idx_savings_log_saved_on ON savings_log (saved_on DESC);

-- ============================================================
-- Seeds
-- ============================================================

-- Default expense categories (8)
INSERT INTO expense_categories (name) VALUES
    ('طعام وشرب'),
    ('مواصلات'),
    ('تعليم وكتب'),
    ('ترفيه وألعاب'),
    ('ملابس'),
    ('تقنية واشتراكات'),
    ('صحة'),
    ('متفرقات')
ON CONFLICT (name) DO NOTHING;

-- Default streak: Duolingo daily at 19:00 Cairo, starts at 0
INSERT INTO streaks (name, reminder_time, reminder_message, current_streak, longest_streak)
VALUES ('ديولينجو', '19:00', 'Duolingo — حافظ على السلسلة! 🔥', 0, 0)
ON CONFLICT (name) DO NOTHING;
