# Aqua Swarm

The AquaButler cluster runs on dedicated hardware separate from home.

## Pool Controller

The pool controller is a balenaOS device running the TypeScript poolController
stack.

## MQTT Broker

Mosquitto terminates TLS at the swarm entrypoint and exposes port 8883.

## Postgres

A 3-node Patroni cluster provides HA Postgres on the Aqua swarm.

## Supabase

Supabase runs on the Aqua swarm with kong, postgrest, gotrue, and realtime.
