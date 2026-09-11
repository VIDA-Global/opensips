-- OpenSIPS-owned placement state. Apply explicitly with the database owner role.
-- Sage's tables and tenant/admission authority are not modified here.
CREATE SCHEMA IF NOT EXISTS opensips_placement;

CREATE TABLE IF NOT EXISTS opensips_placement.control (
    namespace varchar(64) PRIMARY KEY,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    poll_owner varchar(64),
    poll_epoch bigint NOT NULL DEFAULT 0 CHECK (poll_epoch >= 0),
    poll_until timestamptz,
    next_ticket bigint NOT NULL DEFAULT 0,
    installed_ticket bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opensips_placement.nodes (
    namespace varchar(64) NOT NULL REFERENCES opensips_placement.control(namespace),
    node_id varchar(64) NOT NULL,
    identity_sha256 char(64) NOT NULL,
    routing jsonb NOT NULL,
    channel_limit integer NOT NULL CHECK (channel_limit BETWEEN 1 AND 10000),
    eligible_until timestamptz NOT NULL,
    telemetry_generation varchar(128),
    observation_sequence bigint,
    observed_at varchar(64),
    physical_channels integer CHECK (physical_channels BETWEEN 0 AND 10000),
    load_valid boolean NOT NULL DEFAULT false,
    load_until timestamptz,
    sample_not_before timestamptz,
    load_ticket bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, node_id)
);

CREATE TABLE IF NOT EXISTS opensips_placement.reservations (
    namespace varchar(64) NOT NULL REFERENCES opensips_placement.control(namespace),
    allocation_id char(64) NOT NULL,
    request_sha256 char(64),
    product varchar(8) CHECK (product IN ('siprec', 'voice')),
    channels integer CHECK (channels BETWEEN 1 AND 10000),
    node_id varchar(64),
    identity_sha256 char(64),
    routing jsonb,
    state varchar(12) NOT NULL CHECK (state IN ('pending', 'confirmed', 'released')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    confirmed_at timestamptz,
    PRIMARY KEY (namespace, allocation_id),
    CHECK (state = 'released' OR
           (request_sha256 IS NOT NULL AND product IS NOT NULL AND channels IS NOT NULL
            AND node_id IS NOT NULL AND identity_sha256 IS NOT NULL AND routing IS NOT NULL
            AND expires_at IS NOT NULL)),
    CHECK (state <> 'confirmed' OR confirmed_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS opensips_placement.telemetry_history (
    namespace varchar(64) NOT NULL REFERENCES opensips_placement.control(namespace),
    node_id varchar(64) NOT NULL,
    identity_sha256 char(64) NOT NULL,
    telemetry_generation varchar(128) NOT NULL,
    first_ticket bigint NOT NULL,
    PRIMARY KEY(namespace, node_id, identity_sha256, telemetry_generation)
);

CREATE INDEX IF NOT EXISTS live_node_eligibility
    ON opensips_placement.nodes(namespace, eligible_until);

CREATE INDEX IF NOT EXISTS reservation_pending_capacity
    ON opensips_placement.reservations(namespace, expires_at, node_id)
    WHERE state IN ('pending', 'confirmed');
