## ADDED Requirements

### Requirement: Template-keyed score caching
The pipeline SHALL cache anomaly scores by Drain3 template so that repeated log lines sharing a template do not each require model inference.

#### Scenario: Cache hit avoids inference
- **WHEN** a record's template has a valid cached score
- **THEN** the pipeline SHALL use the cached score and SHALL NOT call `detector-service` for that record

#### Scenario: Cache miss is scored and stored
- **WHEN** a record's template has no valid cached entry
- **THEN** the pipeline SHALL score the record through `detector-service` and store the result against that template

#### Scenario: Misses are batched
- **WHEN** multiple cache misses are pending
- **THEN** the pipeline SHALL batch them into a single scoring request within the detector's configured maximum batch size

### Requirement: Model-scoped cache keys
Cached scores SHALL be attributed to the exact model that produced them, so that a model change cannot serve scores from a previous model.

#### Scenario: Cache key includes model identity
- **WHEN** a score is cached
- **THEN** the cache key SHALL include the model key and model revision reported by `detector-service`, in addition to the template identifier

#### Scenario: Model swap invalidates cached scores
- **WHEN** the configured model or its revision changes
- **THEN** previously cached scores SHALL NOT be served, and affected templates SHALL be re-scored

#### Scenario: Stored anomalies carry the producing model
- **WHEN** an anomaly derived from a cached score is persisted
- **THEN** the stored row SHALL record the model key, model revision and threshold that produced the score, matching the provenance guarantee for freshly scored records

### Requirement: Cache bypass for volatile records
Records whose meaning is not determined by their template alone SHALL bypass the cache and always be scored.

#### Scenario: New template always scored
- **WHEN** Drain3 assigns a record a template seen for the first time
- **THEN** the record SHALL be scored rather than served from cache

#### Scenario: Expired entry re-scored
- **WHEN** a cached entry is older than its configured time-to-live
- **THEN** the record SHALL be re-scored and the entry refreshed

#### Scenario: Security-relevant parameters bypass the cache
- **WHEN** a record's extracted Drain3 parameters match the configured volatility denylist, covering at minimum authentication outcomes, HTTP 5xx status codes, non-zero exit codes and kernel-level severities
- **THEN** the record SHALL be scored regardless of any cached entry for its template

#### Scenario: Bypass rate is observable
- **WHEN** an operator inspects pipeline status
- **THEN** the proportion of records bypassing the cache SHALL be reported, so a denylist broad enough to negate the cache is visible

### Requirement: Measured cache effectiveness
The cache hit rate SHALL be measured against representative log data before dependent work proceeds, and SHALL remain observable at runtime.

#### Scenario: Hit rate measured before build-out
- **WHEN** the caching design is evaluated against representative logs
- **THEN** the measurement SHALL report unique templates per thousand lines and the resulting projected hit rate

#### Scenario: Design gate on measured hit rate
- **WHEN** the measured hit rate falls below the agreed threshold
- **THEN** the throughput design SHALL be revisited before further dependent work, rather than proceeding on the assumption that caching closes the throughput gap

#### Scenario: Runtime observability
- **WHEN** the pipeline is running
- **THEN** it SHALL expose the live cache hit rate, entry count, and effective records-per-second, so a degrading hit rate is detectable before it becomes a backlog

### Requirement: Cache is optional
The cache SHALL be disableable by configuration without changing the scoring contract or the stored results.

#### Scenario: Cache disabled
- **WHEN** the cache is disabled by configuration
- **THEN** every record SHALL be scored through `detector-service`, and stored anomalies SHALL be identical in shape to those produced with the cache enabled
