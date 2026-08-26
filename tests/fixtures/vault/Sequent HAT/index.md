# Sequent HAT

The Sequent Microsystems 16-channel relay HAT drives the pool pump,
heater, and valve actuators.

## GPIO

The HAT exposes 16 relays over GPIO 0-15 on the I2C bus.

## I2C Bus

The bus is at /dev/i2c-1 on the Raspberry Pi host.

## SPI Bus

The SPI bus carries temperature sensor reads from the thermocouple board.

## Calibration

Each relay is calibrated on first boot and re-calibrated every 30 days.

## Wiring

The wiring harness terminates in the equipment pad enclosure.
