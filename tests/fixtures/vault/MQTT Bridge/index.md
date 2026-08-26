# MQTT Bridge

The MQTT->PG bridge consumes every MQTT message published by the pool
controller and persists it into the telemetry schema in Postgres.

## Schema

Each bridge message writes one row to telemetry.samples keyed on the
device_id and recorded_at columns.

## Backpressure

The bridge applies consumer-side rate limiting at 200 msgs/sec.

## Reconnect

On broker disconnect the bridge reconnects with exponential backoff up to
60 seconds.
