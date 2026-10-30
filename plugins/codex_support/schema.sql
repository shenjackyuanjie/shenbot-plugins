-- 仅由 Archive.initialize 在单个事务及迁移锁内执行；不改动 DSH 的任何表。
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE {schema}.schema_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE {schema}.jobs (
    id uuid PRIMARY KEY,
    account bigint NOT NULL,
    room_id bigint NOT NULL,
    sender_id bigint NOT NULL,
    source_message_id text NOT NULL,
    original_prompt text NOT NULL,
    reviewed_prompt text,
    approval_digest text,
    -- 创建任务时冻结的生成参数（尺寸、质量、背景），确认阶段只读不写。
    image_params jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    -- 0 为首次确认，只有领取新的显式重试时才递增。
    generation_attempt integer NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'reviewing' CHECK (state IN
        ('reviewing','awaiting_confirmation','generating','succeeded','blocked','failed','unknown','cancelled','expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '10 minutes',
    UNIQUE(account, room_id, source_message_id)
);
CREATE TABLE {schema}.requests (
    id uuid PRIMARY KEY,
    job_id uuid REFERENCES {schema}.jobs(id),
    provider text NOT NULL,
    stage text NOT NULL,
    model text NOT NULL,
    generation_attempt integer NOT NULL DEFAULT 0,
    request_payload jsonb NOT NULL,
    response_payload jsonb,
    state text NOT NULL DEFAULT 'started' CHECK (state IN ('started','succeeded','failed','unknown')),
    http_status integer,
    upstream_request_id text,
    error_code text,
    usage jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
-- 每次明确确认或重试只允许一次收费生成，重复消息及断线恢复不能重发。
CREATE UNIQUE INDEX one_generation_per_attempt ON {schema}.requests(job_id, generation_attempt) WHERE stage = 'generate';
CREATE INDEX requests_by_job ON {schema}.requests(job_id, started_at);
CREATE TABLE {schema}.assets (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
    media_type text NOT NULL,
    width integer NOT NULL CHECK (width > 0),
    height integer NOT NULL CHECK (height > 0),
    bytes integer NOT NULL CHECK (bytes > 0 AND bytes <= 52428800),
    data bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (octet_length(data) = bytes)
);
CREATE TABLE {schema}.job_assets (
    job_id uuid NOT NULL REFERENCES {schema}.jobs(id),
    sha256 text NOT NULL REFERENCES {schema}.assets(sha256),
    role text NOT NULL CHECK (role IN ('reference','original','delivery')),
    -- 一次可以引用多张参考图，position 保留命令里的顺序，改图请求按它对齐。
    position integer NOT NULL DEFAULT 0 CHECK (position >= 0),
    request_id uuid REFERENCES {schema}.requests(id),
    PRIMARY KEY(job_id, role, position)
);
CREATE TABLE {schema}.events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES {schema}.jobs(id),
    kind text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_by_job ON {schema}.events(job_id, id);
COMMENT ON TABLE {schema}.jobs IS 'QQ 生图任务及确认状态；只存命令所需来源，不复制群历史';
COMMENT ON TABLE {schema}.requests IS '每次外部模型/额度请求；外发前提交 started，禁止保存认证头';
COMMENT ON TABLE {schema}.assets IS '参考图、原图与发送副本实际二进制，按 SHA-256 去重';
COMMENT ON TABLE {schema}.events IS '确认、取消、回图及恢复事件；Bridge 受理不等于 QQ 最终送达';
INSERT INTO {schema}.schema_version(version) VALUES (4);
