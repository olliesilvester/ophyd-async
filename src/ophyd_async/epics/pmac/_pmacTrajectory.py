import time

from bluesky.protocols import Flyable, Preparable
from scanspec.specs import Frames

from ophyd_async.core.async_status import AsyncStatus, WatchableAsyncStatus
from ophyd_async.core.signal import observe_value
from ophyd_async.core.utils import WatcherUpdate
from ophyd_async.epics.pmac import Pmac, PmacCSMotor

TICK_S = 0.000001


class PmacTrajectory(Pmac, Flyable, Preparable):
    """Device that moves a PMAC Motor record"""

    def __init__(self, prefix: str, cs: int, name="") -> None:
        # Make a dict of which motors are for which cs axis
        self._fly_start: float
        self.cs = cs
        super().__init__(prefix, cs, name=name)

    async def _ramp_up_velocity_pos(
        self, velocity: float, motor: PmacCSMotor, end_velocity: float
    ):
        # Assuming ramping to or from 0
        max_velocity_acceleration_time = await motor.acceleration_time.get_value()
        max_velocity = await motor.max_velocity.get_value()
        delta_v = abs(end_velocity - velocity)
        accl_time = max_velocity_acceleration_time * delta_v / max_velocity
        disp = 0.5 * (velocity + end_velocity) * accl_time
        return [disp, accl_time]

    @AsyncStatus.wrap
    async def prepare(self, stack: list[Frames[PmacCSMotor]]):
        # Which Axes are in use?

        scanSize = len(stack[0])
        scanAxes = stack[0].axes()

        cs_ports = set()
        self.profile = {}
        for axis in scanAxes:
            if axis != "DURATION":
                await axis.get_cs_info()
                self.profile[axis.cs_axis] = []
                self.profile[axis.cs_axis + "_velocity"] = []
                cs_ports.add(axis.cs_port)
            else:
                self.profile[axis.lower()] = []
        cs_port = cs_ports.pop()

        # Calc Velocity

        for axis in scanAxes:
            for i in range(scanSize):
                if axis != "DURATION":
                    self.profile[axis.cs_axis + "_velocity"].append(
                        (stack[0].upper[axis][i] - stack[0].lower[axis][i])
                        / (stack[0].midpoints["DURATION"][i])
                    )
                    self.profile[axis.cs_axis].append(stack[0].midpoints[axis][i])
                else:
                    self.profile[axis.lower()].append(
                        int(stack[0].midpoints[axis][i] / TICK_S)
                    )

        # Calculate Starting and end Position to allow ramp up and trail off velocity
        self.initial_pos = {}
        run_up_time = 0
        final_time = 0
        for axis in scanAxes:
            if axis != "DURATION":
                run_up_disp, run_up_t = await self._ramp_up_velocity_pos(
                    0,
                    axis,
                    self.profile[axis.cs_axis + "_velocity"][0],
                )
                self.initial_pos[axis.cs_axis] = (
                    self.profile[axis.cs_axis][0] - run_up_disp
                )
                # trail off position and time
                if (
                    self.profile[axis.cs_axis + "_velocity"][0]
                    == self.profile[axis.cs_axis + "_velocity"][-1]
                ):
                    final_pos = self.profile[axis.cs_axis][-1] + run_up_disp
                    final_time = run_up_t
                else:
                    ramp_down_disp, ramp_down_time = await self._ramp_up_velocity_pos(
                        self.profile[axis.cs_axis + "_velocity"][-1],
                        axis,
                        0,
                    )
                    final_pos = self.profile[axis.cs_axis][-1] + ramp_down_disp
                    final_time = max(ramp_down_time, final_time)
                self.profile[axis.cs_axis].append(final_pos)
                self.profile[axis.cs_axis + "_velocity"].append(0)
                run_up_time = max(run_up_time, run_up_t)

        self.profile["duration"][0] += run_up_time / TICK_S
        self.profile["duration"].append(int(final_time / TICK_S))

        # Send trajectory to brick
        for axis in scanAxes:
            if axis != "DURATION":
                self.profile_cs_name.set(cs_port)
                self.points_to_build.set(scanSize + 1)
                getattr(self, "use_" + axis.cs_axis).set(True)
                getattr(self, axis.cs_axis).set(self.profile[axis.cs_axis])
                getattr(self, axis.cs_axis + "_vel").set(
                    self.profile[axis.cs_axis + "_velocity"]
                )
            else:
                self.timeArray.set(self.profile["duration"])

        # MOVE TO START
        for axis in scanAxes:
            if axis != "DURATION":
                await axis.set(self.initial_pos[axis.cs_axis])

        # Set No Of Points

        self.build_profile.set(True)
        self._fly_start = time.monotonic

    @AsyncStatus.wrap
    async def kickoff(self):
        await self.execute_profile.set(True)

    @WatchableAsyncStatus.wrap
    async def complete(self):
        async for percent in observe_value(self.scan_percent):
            yield WatcherUpdate(
                name=self.name,
                current=percent,
                initial=0,
                target=100,
                unit="%",
                precision=0,
                time_elapsed=time.monotonic() - self._fly_start,
            )
