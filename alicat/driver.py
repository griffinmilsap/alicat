"""Python driver for Alicat mass flow controllers, using serial communication.

Distributed under the GNU General Public License v2
Copyright (C) 2023 NuMat Technologies
"""
from typing import Dict, Union

from .util import Client, SerialClient, TcpClient


class FlowMeter:
    """Python driver for Alicat Flow Meters.

    [Reference](http://www.alicat.com/
    products/mass-flow-meters-and-controllers/mass-flow-meters/).

    This communicates with the flow meter over a USB or RS-232/RS-485
    connection using pyserial, or an Ethernet <-> serial converter.
    """

    # A dictionary that maps port names to a tuple of connection
    # objects and the refcounts
    open_ports: Dict[int, tuple] = {}
    gases = ['Air', 'Ar', 'CH4', 'CO', 'CO2', 'C2H6', 'H2', 'He',
             'N2', 'N2O', 'Ne', 'O2', 'C3H8', 'n-C4H10', 'C2H2',
             'C2H4', 'i-C2H10', 'Kr', 'Xe', 'SF6', 'C-25', 'C-10',
             'C-8', 'C-2', 'C-75', 'A-75', 'A-25', 'A1025', 'Star29',
             'P-5']

    def __init__(self, address='/dev/ttyUSB0', unit='A', **kwargs):
        """Connect this driver with the appropriate USB / serial port.

        Args:
            address: The serial port or TCP address:port. Default '/dev/ttyUSB0'.
            unit: The Alicat-specified unit ID, A-Z. Default 'A'.
        """
        if address.startswith('/dev') or address.startswith('COM'):  # serial
            self.hw: Client = SerialClient(address=address, **kwargs)
        else:
            self.hw = TcpClient(address=address, **kwargs)

        self.unit = unit
        self.keys = ['pressure', 'temperature', 'volumetric_flow', 'mass_flow',
                     'setpoint', 'gas']
        self.open = True
        self.firmware: Union[str, None] = None

    async def __aenter__(self, *args):
        """Provide async enter to context manager."""
        return self

    async def __aexit__(self, *args):
        """Provide async exit to context manager."""
        return

    @classmethod
    async def is_connected(cls, port, unit='A') -> bool:
        """Return True if the specified port is connected to this device.

        This class can be used to automatically identify ports with connected
        Alicats. Iterate through all connected interfaces, and use this to
        test. Ports that come back True should be valid addresses.

        Note that this distinguishes between `FlowController` and `FlowMeter`.
        """
        is_device = False
        try:
            device = cls(port, unit)
            try:
                c = await device.get()
                if cls.__name__ == 'FlowMeter':
                    assert c
                    assert 'setpoint' not in device.keys
                elif cls.__name__ == 'FlowController':
                    assert c
                    assert 'setpoint' in device.keys
                else:
                    raise NotImplementedError('Must be meter or controller.')
                is_device = True
            finally:
                await device.close()
        except Exception:
            pass
        return is_device

    def _test_controller_open(self) -> None:
        """Raise an IOError if the FlowMeter has been closed.

        Does nothing if the meter is open and good for read/write
        otherwise raises an IOError. This only checks if the meter
        has been closed by the FlowMeter.close method.
        """
        if not self.open:
            raise OSError(f"The FlowMeter with unit ID {self.unit} and "
                           "port {self.hw.address} is not open")

    def _is_float(self, msg):
        try:
            float(msg)
            return True
        except ValueError:
            return False

    async def get(self) -> dict:
        """Get the current state of the flow controller.

        From the Alicat mass flow controller documentation, this data is:
         * Pressure (normally in psia)
         * Temperature (normally in C)
         * Volumetric flow (in units specified at time of order)
         * Mass flow (in units specified at time of order)
         * Total flow (only on models with the optional totalizer function)
         * Currently selected gas

        Args:
            retries: Number of times to re-attempt reading. Default 2.
        Returns:
            The state of the flow controller, as a dictionary.

        """
        self._test_controller_open()

        command = f'{self.unit}'
        line = await self.hw._write_and_read(command)
        spl = line.split()
        unit, values = spl[0], spl[1:]

        # Mass/volume over range error.
        # Explicitly silenced because I find it redundant.
        while values[-1].upper() in ['MOV', 'VOV', 'POV']:
            del values[-1]
        if unit != self.unit:
            raise ValueError("Flow controller unit ID mismatch.")
        if values[-1].upper() == 'LCK':
            self.button_lock = True
            del values[-1]
        else:
            self.button_lock = False
        if len(values) == 5 and len(self.keys) == 6:
            del self.keys[-2]
        elif len(values) == 7 and len(self.keys) == 6:
            self.keys.insert(5, 'total flow')
        elif len(values) == 2 and len(self.keys) == 6:
            self.keys.insert(1, 'setpoint')
        return {k: (float(v) if self._is_float(v) else v)
                for k, v in zip(self.keys, values)}

    async def set_gas(self, gas):
        """Set the gas type.

        Args:
            gas: The gas type, as a string or integer. Supported strings are:
                'Air', 'Ar', 'CH4', 'CO', 'CO2', 'C2H6', 'H2', 'He', 'N2',
                'N2O', 'Ne', 'O2', 'C3H8', 'n-C4H10', 'C2H2', 'C2H4',
                'i-C2H10', 'Kr', 'Xe', 'SF6', 'C-25', 'C-10', 'C-8', 'C-2',
                'C-75', 'A-75', 'A-25', 'A1025', 'Star29', 'P-5'

                Gas mixes may only be called by their mix number.
        """
        self._test_controller_open()

        if isinstance(gas, str):
            if gas not in self.gases:
                raise ValueError(f"{gas} not supported!")
            gas_number = self.gases.index(gas)
        else:
            gas_number = gas
        command = f'{self.unit}$$W46={gas_number}'
        await self.hw._write_and_read(command)
        reg46 = await self.hw._write_and_read('f{self.unit}$$R46')
        reg46_gasbit = int(reg46.split()[-1]) & 0b0000000111111111

        if gas_number != reg46_gasbit:
            raise OSError("Cannot set gas.")

    async def create_mix(self, mix_no, name, gases) -> None:
        """Create a gas mix.

        Gas mixes are made using COMPOSER software.
        COMPOSER mixes are only allowed for firmware 5v or greater.

        Args:
        mix_no: The mix number. Gas mixes are stored in slots 236-255.
        name: A name for the gas that will appear on the front panel.
        Names greater than six letters will be cut off.
        gases: A dictionary of the gas by name along with the associated
        percentage in the mix.
        """
        self._test_controller_open()

        firmware = await self.get_firmware()
        if any(v in firmware for v in ['2v', '3v', '4v', 'GP']):
            raise OSError("This unit does not support COMPOSER gas mixes.")

        if mix_no < 236 or mix_no > 255:
            raise ValueError("Mix number must be between 236-255!")

        total_percent = sum(gases.values())
        if total_percent != 100:
            raise ValueError("Percentages of gas mix must add to 100%!")

        if any(gas not in self.gases for gas in gases):
            raise ValueError("Gas not supported!")

        gas_list = ' '.join(
            [' '.join([str(percent), str(self.gases.index(gas))])
                for gas, percent in gases.items()])
        command = ' '.join([self.unit,
                            'GM',
                            name,
                            str(mix_no),
                            gas_list])

        line = await self.hw._write_and_read(command)

        # If a gas mix is not successfully created, ? is returned.
        if line == '?':
            raise OSError("Unable to create mix.")

    async def delete_mix(self, mix_no) -> None:
        """Delete a gas mix."""
        self._test_controller_open()
        command = f'{self.unit}GD{mix_no}'
        line = await self.hw._write_and_read(command)

        if line == '?':
            raise OSError("Unable to delete mix.")

    async def lock(self) -> None:
        """Lock the buttons."""
        self._test_controller_open()
        command = f'{self.unit}$$L'
        await self.hw._write_and_read(command)

    async def unlock(self) -> None:
        """Unlock the buttons."""
        self._test_controller_open()
        command = f'{self.unit}$$U'
        await self.hw._write_and_read(command)

    async def is_locked(self) -> bool:
        """Return whether the buttons are locked."""
        await self.get()
        return self.button_lock

    async def tare_pressure(self) -> None:
        """Tare the pressure."""
        self._test_controller_open()

        command = f'{self.unit}$$PC'
        line = await self.hw._write_and_read(command)

        if line == '?':
            raise OSError("Unable to tare pressure.")

    async def tare_volumetric(self) -> None:
        """Tare volumetric flow."""
        self._test_controller_open()
        command = f'{self.unit}$$V'
        line = await self.hw._write_and_read(command)

        if line == '?':
            raise OSError("Unable to tare flow.")

    async def reset_totalizer(self) -> None:
        """Reset the totalizer."""
        self._test_controller_open()
        command = f'{self.unit}T'
        await self.hw._write_and_read(command)

    async def get_firmware(self) -> str:
        """Get the device firmware version."""
        if self.firmware is None:
            self._test_controller_open()
            command = f'{self.unit}VE'
            self.firmware = await self.hw._write_and_read(command)
        return self.firmware

    async def flush(self) -> None:
        """Read all available information. Use to clear queue."""
        self._test_controller_open()

        await self.hw._clear()

    async def close(self) -> None:
        """Close the flow meter. Call this on program termination.

        Also closes the serial port if no other FlowMeter object has
        a reference to the port.
        """
        if not self.open:
            return
        self.hw.close
        self.open = False


class FlowController(FlowMeter):
    """Python driver for Alicat Flow Controllers.

    [Reference](http://www.alicat.com/products/mass-flow-meters-and-
    controllers/mass-flow-controllers/).

    This communicates with the flow controller over a USB or RS-232/RS-485
    connection using pyserial.

    To set up your Alicat flow controller, power on the device and make sure
    that the "Input" option is set to "Serial".
    """

    registers = {'mass flow': 0b00100101, 'vol flow': 0b00100100,
                 'abs pressure': 0b00100010, 'gauge pressure': 0b00100110,
                 'diff pressure': 0b00100111}

    def __init__(self, address='/dev/ttyUSB0', unit='A'):
        """Connect this driver with the appropriate USB / serial port.

        Args:
            address: The serial port or TCP address:port. Default '/dev/ttyUSB0'.
            unit: The Alicat-specified unit ID, A-Z. Default 'A'.
        """
        FlowMeter.__init__(self, address, unit)
        # try:
        #     self.control_point = self._get_control_point()
        # except Exception:
        #     self.control_point = None
        self.control_point = None

    async def get(self) -> dict:
        """Get the current state of the flow controller.

        From the Alicat mass flow controller documentation, this data is:
         * Pressure (normally in psia)
         * Temperature (normally in C)
         * Volumetric flow (in units specified at time of order)
         * Mass flow (in units specified at time of order)
         * Flow setpoint (in units of control point)
         * Flow control point (either 'flow' or 'pressure')
         * Total flow (only on models with the optional totalizer function)
         * Currently selected gas

        Returns:
            The state of the flow controller, as a dictionary.

        """
        state = await super().get()
        if state is None:
            return None
        state['control_point'] = self.control_point
        return state

    async def set_flow_rate(self, flowrate) -> None:
        """Set the target flow rate.

        Args:
            flow: The target flow rate, in units specified at time of purchase
        """
        if self.control_point in ['abs pressure', 'gauge pressure', 'diff pressure']:
            await self._set_setpoint(0)
            await self._set_control_point('mass flow')
        await self._set_setpoint(flowrate)

    async def set_pressure(self, pressure) -> None:
        """Set the target pressure.

        Args:
            pressure: The target pressure, in units specified at time of
                purchase. Likely in psia.
        """
        if self.control_point in ['mass flow', 'vol flow']:
            await self._set_setpoint(0)
            await self._set_control_point('abs pressure')
        await self._set_setpoint(pressure)

    async def hold(self) -> None:
        """Override command to issue a valve hold.

        For a single valve controller, hold the valve at the present value.
        For a dual valve flow controller, hold the valve at the present value.
        For a dual valve pressure controller, close both valves.
        """
        self._test_controller_open()
        command = f'{self.unit}$$H'
        await self.hw._write_and_read(command)

    async def cancel_hold(self) -> None:
        """Cancel valve hold."""
        self._test_controller_open()
        command = f'{self.unit}$$C'
        await self.hw._write_and_read(command)

    async def get_pid(self) -> dict:
        """Read the current PID values on the controller.

        Values include the loop type, P value, D value, and I value.
        Values returned as a dictionary.
        """
        self._test_controller_open()

        self.pid_keys = ['loop_type', 'P', 'D', 'I']

        command = f'{self.unit}$$r85'
        read_loop_type = await self.hw._write_and_read(command)
        spl = read_loop_type.split()

        loopnum = int(spl[3])
        loop_type = ['PD/PDF', 'PD/PDF', 'PD2I'][loopnum]
        pid_values = [loop_type]
        for register in range(21, 24):
            value = await self.hw._write_and_read(f'{self.unit}$$r{register}')
            value_spl = value.split()
            pid_values.append(value_spl[3])

        return {k: (v if k == self.pid_keys[-1] else str(v))
                for k, v in zip(self.pid_keys, pid_values)}

    async def set_pid(self, p=None, i=None, d=None, loop_type=None) -> None:
        """Set specified PID parameters.

        Args:
            p: Proportional gain
            i: Integral gain. Only used in PD2I loop type.
            d: Derivative gain
            loop_type: Algorithm option, either 'PD/PDF' or 'PD2I'

        This communication works by writing Alicat registers directly.
        """
        self._test_controller_open()
        if loop_type is not None:
            options = ['PD/PDF', 'PD2I']
            if loop_type not in options:
                raise ValueError(f'Loop type must be {options[0]} or {options[1]}.')
            loop_num=options.index(loop_type) + 1
            command = f'{self.unit}$$w85={loop_num}'
            await self.hw._write_and_read(command)
        if p is not None:
            command = f'{self.unit}$$w21={p}'
            await self.hw._write_and_read(command)
        if i is not None:
            command = f'{self.unit}$$w23={i}'
            await self.hw._write_and_read(command)
        if d is not None:
            command = f'{self.unit}$$w22={d}'
            await self.hw._write_and_read(command)

    async def _set_setpoint(self, setpoint) -> None:
        """Set the target setpoint.

        Called by `set_flow_rate` and `set_pressure`, which both use the same
        command once the appropriate register is set.
        """
        self._test_controller_open()

        command = f'{self.unit}S{setpoint:.2f}'
        line = await self.hw._write_and_read(command)
        try:
            current = float(line.split()[5])
        except IndexError:
            current = None
        if current is not None and abs(current - setpoint) > 0.01:
            raise OSError("Could not set setpoint.")

    async def _get_control_point(self):
        """Get the control point, and save to internal variable."""
        command = f'{self.unit}R122'
        line = await self.hw._write_and_read(command)
        if not line:
            return None
        value = int(line.split('=')[-1])
        try:
            return next(p for p, r in self.registers.items() if value == r)
        except StopIteration:
            raise ValueError(f"Unexpected register value: {value:d}")

    async def _set_control_point(self, point) -> None:
        """Set whether to control on mass flow or pressure.

        Args:
            point: Either "flow" or "pressure".
        """
        if point not in self.registers:
            raise ValueError("Control point must be 'flow' or 'pressure'.")
        reg = self.registers[point]
        command = f'{self.unit}W122={reg:d}'
        line = await self.hw._write_and_read(command)

        value = int(line.split('=')[-1])
        if value != reg:
            raise OSError("Could not set control point.")
        self.control_point = point
