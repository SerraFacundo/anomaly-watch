## ADDED Requirements

### Requirement: Authentication documentation
The architecture document SHALL describe the JWT-based authentication mechanism used by the API service.

#### Scenario: JWT auth documented
- **WHEN** a contributor reads the auth/security section
- **THEN** it SHALL describe JWT token-based authentication with bcrypt password hashing, without external OAuth providers

### Requirement: Role-based access control documentation
The architecture document SHALL describe the RBAC model with three defined roles and their permissions.

#### Scenario: Roles and permissions documented
- **WHEN** a contributor reads the auth/security section
- **THEN** it SHALL list three roles: admin (full access, user management, configuration), analyst (view dashboard, manage alerts, generate reports), viewer (read-only dashboard access)

### Requirement: Security considerations documentation
The architecture document SHALL include a security considerations section covering data protection for the self-hosted deployment model.

#### Scenario: Security section present
- **WHEN** a contributor reads the security section
- **THEN** it SHALL address at minimum: data encryption (TLS in transit), log data privacy (stays on PyME network), and API authentication requirements
