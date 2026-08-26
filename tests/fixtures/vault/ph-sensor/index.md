# PH Sensor

The pH sensor is an Atlas Scientific EZO-pH board connected over I2C.

## Calibration

Calibration requires three buffer solutions: pH 4.0, pH 7.0, and pH 10.0.
Run the calibration routine once per month during scheduled maintenance.

## Wiring

The sensor connects to the I2C bus at address 0x63 on the Sequent HAT.

## Reading

The ph level sensor returns a voltage between 0V and 3.3V, which the EZO
board converts to a pH value between 0 and 14.

## Storage

Calibration coefficients are stored in non-volatile memory on the EZO board.
