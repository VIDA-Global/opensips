# Native SIP Ownership

Native OpenSIPS uses one active process per ownership namespace and cold standbys.
PostgreSQL owns the immutable instance binding, epoch, pending transfer and retired
instance tombstones. There is no expiring ownership lease: loss of a database or
cluster connection alone never authorizes a second process.

## Fence Before Promote

The independent lifecycle controller invokes
[`ownership_controller.py`](../scripts/ownership_controller.py) with an approved
manifest, desired instance, expected epoch and stable operation UUID:

```sh
python3 image/scripts/ownership_controller.py \
  --config /etc/telephony/ownership-controller.json \
  --candidate i-00000000000000002 \
  --expected-epoch 1 \
  --operation-id 11111111-1111-4111-8111-111111111111
```

This command **terminates the previous OpenSIPS EC2 instance**. The manifest
restricts account, region and instance IDs; IAM independently restricts the region
and `OpenSIPSOwnershipNamespace` tag. Keep this controller outside the instance it
can fence and do not give its database-write or termination credentials to SIP
processes. DevOps owns candidate selection, ASG membership and frontend steering.
The command performs the complete fenced transfer after that desired-state input.

1. Lock the namespace and commit a pending operation with its exact old/candidate
   identities. Concurrent or stale requests cannot replace that operation.
2. Release the SQL transaction and connection, then fence the old instance.
3. Observe the exact instance in the expected account as **terminated**. An API
   acknowledgement, timeout, lost heartbeat, stopped/hibernated instance or local
   sharing-tag change is insufficient.
4. In a new transaction, recheck the operation and epoch, retire the old identity
   permanently and grant the candidate the next epoch atomically.

Retry an uncertain operation with the same UUID, candidate and expected epoch.
A crash after fencing leaves the operation resumable; a lost completion response
replays the completed result without another fence. An unavailable fence leaves
promotion blocked. Instance IDs bind permanently to one namespace and retired IDs
cannot be recycled into another namespace.

## Native Startup And Database Permissions

The installed systemd unit runs
[`ownership_guard.py`](../scripts/ownership_guard.py) before OpenSIPS configuration
checking or execution. It obtains the local instance ID through bounded IMDSv2 and
checks current SQL authority through the validated `placement.json` database and
namespace. Missing schema, denied credentials, lost SQL connectivity, pending
transfer or wrong/retired identity prevents startup. The unit has a 15-second
startup bound. `state_owner` must be `active` in prepared configuration; it is not
a promotion control. A standby does not run an OpenSIPS process with replicated
timers that could emit independently.

As the database administrator, install
[`ownership-schema.sql`](../assets/ownership-schema.sql) once in the designated
database and provision each namespace explicitly. Use separate roles, for example:

```sql
INSERT INTO opensips_ownership.authority(namespace) VALUES ('telephony');
GRANT USAGE ON SCHEMA opensips_ownership TO placement, ownership_controller;
GRANT SELECT ON opensips_ownership.authority,
  opensips_ownership.retired_instances TO placement;
GRANT SELECT ON ALL TABLES IN SCHEMA opensips_ownership TO ownership_controller;
GRANT UPDATE (epoch, owner_instance, candidate_instance, operation_id, phase)
  ON opensips_ownership.authority TO ownership_controller;
GRANT INSERT ON opensips_ownership.instance_namespaces,
  opensips_ownership.retired_instances TO ownership_controller;
```

The runtime role gets no ownership writes, and the controller gets no deletion of
retirement evidence. Initial activation uses epoch zero and fences no previous
instance. The namespace must already exist. Configure ASG health/replacement policy
to tolerate cold standbys: SIP readiness must not cause a deliberately inactive
instance to churn. NLB routing must select the active, ready instance. These AWS
configuration responsibilities are part of deployment qualification.

## Local Evidence And AWS Qualification

`make -C image test-ownership-proof` runs real PostgreSQL and stock OpenSIPS in a
disposable internal Docker network. It severs the old process's real SQL TCP link
while that process remains alive and sends SIP and media-control requests. An
unavailable fencer blocks promotion. The independent test fencer then kills and
verifies cessation of the complete old process group before SQL grants epoch two.
Only the replacement can subsequently emit. Independent SQL contenders, completed
replay, namespace binding, retirement and runtime-role write denial are checked.
The fixture uses a media-control observer; it does not claim recorded-audio or
confirmed-dialog continuity from these packets.

The EC2 adapter has deterministic tests for scope and terminal-state observation.
It has not been exercised against AWS. Native AMI boot, IAM enforcement, actual
instance termination, ASG replacement, stable NLB identities, dialog restoration
and post-transfer media require the approved AWS environment. Cold startup does
not preserve early SIP transactions, TCP/TLS sessions or process memory, and does
not itself implement RTPengine media recovery. See [media recovery](media-recovery.md).
