# B2BUA And Media Recovery Design

## Supported HA Boundary

The image contains `proto_bin`, `clusterer`, `dialog`, `b2b_entities`, and `b2b_logic`. These modules provide the building blocks for active-active replication of confirmed dialog and B2B state. Deployment configuration must still define node identity, private BIN peers, stable listener tags, database behavior, frontend traffic steering, and B2B routes.

The supported baseline claim is:

> A surviving OpenSIPS node can provide new-call service and best-effort routing for a replicated confirmed dialog when its original RTPengine and SIP endpoints remain reachable.

It does not preserve an early INVITE transaction, retransmission cache, process-local value, TCP connection, TLS session, or a failed RTPengine's media state.

## Why Detection Is Not Recovery

OpenSIPS can detect a failed RTPengine control endpoint and stop assigning new calls to it. A different RTPengine does not possess the failed relay's allocated ports, learned endpoint addresses, SDP state, ICE state, SRTP keys, recording state, or packet-forwarding rules. Selecting another control socket for a later command therefore cannot move existing media.

A fresh offer/answer exchange can establish replacement media, but it changes the call rather than restoring relay memory. Both endpoints must support and accept a mid-dialog re-INVITE, and the signaling controller must coordinate two independently versioned dialog legs.

## Required Architecture

Automatic recovery requires all of the following outside this AMI:

- Every call is anchored through a reviewed `b2b_entities` and `b2b_logic` scenario.
- B2B state is replicated to a healthy OpenSIPS peer.
- The deployment stores the selected RTPengine identity and current negotiated SDP state in replicated B2B/dialog state.
- At least one replacement RTPengine is healthy and reachable from the surviving OpenSIPS node.
- Both SIP endpoints permit proxy/B2BUA-originated re-INVITEs and the resulting codec, ICE, and SRTP changes.
- The B2BUA serializes offer/answer operations and handles `491 Request Pending` glare.
- Ingress traffic reaches the surviving node using stable advertised identities.
- Monitoring can distinguish control failure, media failure, renegotiation progress, rollback, and call termination.

For stronger continuity, prefer RTPengine-level HA that preserves or reconstructs media sessions. B2BUA renegotiation remains a recovery fallback, not a substitute for relay HA.

## Recovery State Machine

The deployment-specific B2B policy should use a single owner and explicit states:

```text
established
  -> failure-suspected
  -> replacement-reserved
  -> leg-a-offer-pending
  -> leg-b-answer-pending
  -> commit-pending
  -> recovered

Any pending state
  -> rollback-pending
  -> established-on-original, if the original relay recovered
  -> terminating, when rollback or either endpoint fails
```

The owner must:

1. Confirm failure using bounded control probes and avoid reacting to one transient timeout.
2. Fence concurrent recovery for the same B2B session using replicated ownership plus an external partition policy.
3. Reserve a replacement relay without deleting the original session.
4. Generate an offer acceptable to the first leg and persist the pending CSeq and operation identity.
5. Apply the accepted SDP to the second leg and wait for its final answer.
6. Commit the new relay identity only after both legs complete consistently.
7. Delete old media idempotently after commit if the old relay becomes reachable.
8. Roll back both legs when possible; otherwise terminate the call rather than leave asymmetric or insecure media.

Every transition needs a bounded timer, correlation identity, idempotent retry behavior, and metrics. A node takeover may resume only from a replicated transition whose pending SIP transaction semantics are known. Because ordinary TM transactions are not replicated, takeover during an active recovery transaction generally requires timeout and a new, standards-compliant attempt rather than pretending the transaction survived.

## Mandatory Failure Tests

Use `tests/integration/ha-media-boundary.sh` with an environment-owned SIP test command. The external harness must cover:

- OpenSIPS node loss before and after dialog confirmation.
- Original RTPengine control timeout while media still flows.
- Complete RTPengine process and host loss.
- Replacement allocation failure.
- First-leg and second-leg re-INVITE rejection.
- `491` glare and retry timing.
- Endpoint timeout and disconnect.
- Codec incompatibility.
- ICE restart and no-ICE endpoints.
- SRTP key replacement and downgrade rejection.
- OpenSIPS takeover during each recovery state.
- Duplicate failure events and idempotent cleanup.
- Original RTPengine recovery before and after commit.

Do not enable automatic B2BUA media recovery by default until these tests pass against every supported endpoint family and the production RTPengine topology. The AMI deliberately installs the modules but leaves application routing and automatic recovery disabled.
