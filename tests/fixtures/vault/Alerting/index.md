# Alerting

Alerts flow from the Sequent HAT relays through MQTT to the alerting bridge.

## PagerDuty

The bridge publishes to PagerDuty when the ORP sensor drifts above 850mV.

## Slack

The same bridge posts to the #aqua-alerts channel on Slack.

## Email

A digest email is sent at 08:00 UTC every day.
