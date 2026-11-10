## 1. Reporter

- [x] 1.1 `services/collector/heartbeat.py`: read last sightings, build payloads (split at 1000), post, summarize rejections.
- [x] 1.2 `Settings`: `ANOMALYWATCH_API_URL`, `COLLECTOR_CODE`, `COLLECTOR_TOKEN`, `COLLECTOR_HEARTBEAT_INTERVAL_S`; enabled only with the first three.
- [x] 1.3 `app.py`: heartbeat task beside the pipeline loop; `node_heartbeat` in `GET /status`.
- [x] 1.4 `docker-compose.yml`: the four variables on `collector`, empty by default.

## 2. Tests and docs

- [x] 2.1 `tests/test_collector_heartbeat.py` with fake reader and poster.
- [x] 2.2 `dev` dependency group with pytest; `unit-tests` CI job.
- [x] 2.3 README: configuration and where rejected hosts show up.
- [ ] 2.4 End-to-end run against an AnomalyWatch build that includes `add-node-heartbeat-and-alert-risk`.
