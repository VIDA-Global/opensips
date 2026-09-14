-- Install as an administrative role. The OpenSIPS runtime gets SELECT only;
-- the independent fencing controller gets SELECT/INSERT/UPDATE on this schema.
CREATE SCHEMA IF NOT EXISTS opensips_ownership;
CREATE TABLE opensips_ownership.authority (
    namespace text PRIMARY KEY,
    epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    owner_instance text,
    candidate_instance text,
    operation_id uuid,
    phase text NOT NULL DEFAULT 'idle' CHECK (phase IN ('idle', 'active', 'fencing')),
    CHECK (namespace ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    CHECK (owner_instance IS NULL OR owner_instance ~ '^i-[a-f0-9]{17}$'),
    CHECK (candidate_instance IS NULL OR candidate_instance ~ '^i-[a-f0-9]{17}$'),
    CHECK (
        (phase = 'idle' AND owner_instance IS NULL AND candidate_instance IS NULL AND epoch = 0)
        OR (phase = 'active' AND owner_instance IS NOT NULL AND candidate_instance IS NULL AND epoch > 0)
        OR (phase = 'fencing' AND candidate_instance IS NOT NULL AND operation_id IS NOT NULL)
    )
);
CREATE TABLE opensips_ownership.instance_namespaces (
    instance_id text PRIMARY KEY CHECK (instance_id ~ '^i-[a-f0-9]{17}$'),
    namespace text NOT NULL REFERENCES opensips_ownership.authority(namespace)
);
CREATE TABLE opensips_ownership.retired_instances (
    namespace text NOT NULL REFERENCES opensips_ownership.authority(namespace),
    instance_id text PRIMARY KEY REFERENCES opensips_ownership.instance_namespaces(instance_id),
    retired_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON SCHEMA opensips_ownership FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA opensips_ownership FROM PUBLIC;
