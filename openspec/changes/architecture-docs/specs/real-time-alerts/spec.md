## ADDED Requirements

### Requirement: SSE real-time updates documentation
The architecture document SHALL describe the SSE (Server-Sent Events) mechanism used by the API to push real-time anomaly alerts and dashboard updates to connected clients.

#### Scenario: SSE mechanism documented
- **WHEN** a contributor reads the real-time alerts section
- **THEN** it SHALL describe the API service exposing SSE endpoints for real-time event streaming to the React dashboard and mobile app

### Requirement: Notification channels documentation
The architecture document SHALL describe the notifier service and its supported notification channels for alerting PyME staff about detected anomalies.

#### Scenario: Notification channels listed
- **WHEN** a contributor reads the real-time alerts section
- **THEN** it SHALL list email (SMTP) and webhook as supported notification channels, with additional channels marked as TBD

### Requirement: Alert lifecycle documentation
The architecture document SHALL describe the lifecycle of an alert from detection through notification and acknowledgment.

#### Scenario: Alert lifecycle flow documented
- **WHEN** a contributor reads the real-time alerts section
- **THEN** it SHALL describe the flow: anomaly detected → stored in Postgres → SSE push to dashboard → notification sent via notifier → user acknowledges via API
