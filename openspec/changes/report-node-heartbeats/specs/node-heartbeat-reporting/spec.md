## ADDED Requirements

### Requirement: The collector reports when each host was last heard from
When configured with an AnomalyWatch API URL, a collector code and that collector's agent credential, `log-collector` SHALL send a collector heartbeat to AnomalyWatch at a fixed interval (60 seconds by default). Each heartbeat SHALL carry one entry per host that has reported through a non-revoked agent, with the host name and the latest `last_seen` instant of that host's agents in UTC. A tick with more hosts than AnomalyWatch accepts in one heartbeat (1000) SHALL be split into several heartbeats.

#### Scenario: Hosts are reported with their last sighting
- **WHEN** two agents of host `web-01` were last seen at 22:10 and 22:12, and the heartbeat runs at 22:14
- **THEN** the heartbeat carries `web-01` once, with `lastSeenAt` 22:12 UTC

#### Scenario: Revoked and silent agents are left out
- **WHEN** an agent is revoked, or has never shipped a batch
- **THEN** its host is not reported through that agent

#### Scenario: No hosts yet
- **WHEN** no agent has reported yet
- **THEN** the collector still sends its heartbeat, with an empty node list

#### Scenario: A large fleet
- **WHEN** 1001 hosts have reported
- **THEN** the tick sends two heartbeats, of 1000 and 1 nodes

### Requirement: Reporting never affects detection
Node heartbeat reporting SHALL be disabled unless the API URL, collector code and credential are all configured. A failure to read sightings or to reach AnomalyWatch SHALL NOT stop or slow the scoring pipeline; it SHALL be retried on the next tick and its last error, without the credential, SHALL be visible in `GET /status`. Hosts AnomalyWatch does not accept SHALL be listed in `GET /status` by outcome.

#### Scenario: Not configured
- **WHEN** any of `ANOMALYWATCH_API_URL`, `COLLECTOR_CODE` or `COLLECTOR_TOKEN` is empty
- **THEN** no heartbeat is sent and `GET /status` reports `node_heartbeat.enabled` false

#### Scenario: AnomalyWatch is down
- **WHEN** the heartbeat request fails
- **THEN** the pipeline keeps scoring, `GET /status` shows the error without the credential, and the next tick tries again

#### Scenario: A host is not registered in AnomalyWatch
- **WHEN** AnomalyWatch answers `UNKNOWN_HOST` for `ghost-01`
- **THEN** `GET /status` lists `ghost-01` under `node_heartbeat.rejected.UNKNOWN_HOST`
