-- =============================================================================
-- Video Hosting & Streaming Platform Database Schema
-- Includes PostgreSQL / Supabase Row Level Security (RLS) Policies
-- =============================================================================

-- 1. CDN Accounts Table
CREATE TABLE IF NOT EXISTS cdn_accounts (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    provider VARCHAR(50) NOT NULL DEFAULT 'Hack Club CDN',
    encrypted_credentials TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Videos Table
CREATE TABLE IF NOT EXISTS videos (
    id VARCHAR(36) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    original_filename VARCHAR(255),
    original_size BIGINT DEFAULT 0,
    encoded_size BIGINT DEFAULT 0,
    duration DOUBLE PRECISION DEFAULT 0.0,
    source_width INTEGER DEFAULT 0,
    source_height INTEGER DEFAULT 0,
    source_fps DOUBLE PRECISION DEFAULT 0.0,
    status VARCHAR(50) NOT NULL DEFAULT 'processing',
    cdn_account_id VARCHAR(36) REFERENCES cdn_accounts(id) ON DELETE SET NULL,
    cdn_prefix VARCHAR(255),
    master_playlist_url TEXT,
    thumbnail_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Video Quality Variants Table
CREATE TABLE IF NOT EXISTS video_variants (
    id VARCHAR(36) PRIMARY KEY,
    video_id VARCHAR(36) NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    resolution VARCHAR(20) NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    playlist_url TEXT,
    bitrate INTEGER DEFAULT 0,
    file_size BIGINT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. Video Files Tracking Table (Playlists, Segments, Thumbnails)
CREATE TABLE IF NOT EXISTS video_files (
    id VARCHAR(36) PRIMARY KEY,
    video_id VARCHAR(36) NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    video_variant_id VARCHAR(36) REFERENCES video_variants(id) ON DELETE SET NULL,
    cdn_account_id VARCHAR(36) NOT NULL REFERENCES cdn_accounts(id) ON DELETE CASCADE,
    remote_path TEXT NOT NULL,
    remote_url TEXT NOT NULL,
    file_size BIGINT DEFAULT 0,
    file_type VARCHAR(50) NOT NULL,
    upload_status VARCHAR(50) NOT NULL DEFAULT 'uploaded',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP WITH TIME ZONE
);

-- 5. Jobs Queue Table
CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(36) PRIMARY KEY,
    video_id VARCHAR(36) NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    job_type VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'queued',
    progress DOUBLE PRECISION DEFAULT 0.0,
    current_step VARCHAR(100) DEFAULT 'Queued',
    current_message TEXT DEFAULT '',
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE
);

-- 6. Job Logs Table (Verbose Live Execution Logs)
CREATE TABLE IF NOT EXISTS job_logs (
    id VARCHAR(36) PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    level VARCHAR(20) DEFAULT 'INFO',
    message TEXT NOT NULL,
    metadata_json TEXT
);

-- 7. Settings Table (Runtime Processing Controls)
CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 8. Storage Snapshots Table
CREATE TABLE IF NOT EXISTS storage_snapshots (
    id VARCHAR(36) PRIMARY KEY,
    cdn_account_id VARCHAR(36) NOT NULL REFERENCES cdn_accounts(id) ON DELETE CASCADE,
    used_bytes BIGINT DEFAULT 0,
    available_bytes BIGINT DEFAULT 0,
    total_bytes BIGINT DEFAULT 0,
    checked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Default Settings Seed Data
INSERT INTO settings (key, value) VALUES ('ffmpeg_threads', '40') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('max_concurrent_jobs', '1') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('ffmpeg_preset', 'veryfast') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('ffmpeg_crf', '23') ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('hls_segment_duration', '6') ON CONFLICT (key) DO NOTHING;


-- =============================================================================
-- ROW LEVEL SECURITY (RLS) CONFIGURATION (PostgreSQL / Supabase)
-- Enable RLS on all platform tables and enforce authenticated user access
-- =============================================================================

-- Enable Row Level Security on all tables
ALTER TABLE cdn_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE videos ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_variants ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_snapshots ENABLE ROW LEVEL SECURITY;

-- 1. cdn_accounts RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to cdn_accounts" ON cdn_accounts;
CREATE POLICY "Authenticated users full access to cdn_accounts"
    ON cdn_accounts FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 2. videos RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to videos" ON videos;
CREATE POLICY "Authenticated users full access to videos"
    ON videos FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 3. video_variants RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to video_variants" ON video_variants;
CREATE POLICY "Authenticated users full access to video_variants"
    ON video_variants FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 4. video_files RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to video_files" ON video_files;
CREATE POLICY "Authenticated users full access to video_files"
    ON video_files FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 5. jobs RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to jobs" ON jobs;
CREATE POLICY "Authenticated users full access to jobs"
    ON jobs FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 6. job_logs RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to job_logs" ON job_logs;
CREATE POLICY "Authenticated users full access to job_logs"
    ON job_logs FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 7. settings RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to settings" ON settings;
CREATE POLICY "Authenticated users full access to settings"
    ON settings FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- 8. storage_snapshots RLS Policies
DROP POLICY IF EXISTS "Authenticated users full access to storage_snapshots" ON storage_snapshots;
CREATE POLICY "Authenticated users full access to storage_snapshots"
    ON storage_snapshots FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
