"""Combined analysis of one or more flight logs."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..blackbox.parser import FlightLog
from .behaviour import BehaviourReport, analyze_behaviour
from .flight_data import FlightData, prepare
from .noise import NoiseReport, analyze_noise
from .step_response import StepResponse, analyze_step_responses


@dataclass
class LogAnalysis:
    log: FlightLog
    data: FlightData
    noise: NoiseReport
    steps: list[StepResponse]
    behaviour: BehaviourReport
    warnings: list[str] = field(default_factory=list)

    def step(self, axis: str) -> StepResponse | None:
        return next((s for s in self.steps if s.axis == axis), None)

    def to_dict(self) -> dict:
        return {
            "log_index": self.log.index,
            "firmware": self.log.firmware_revision,
            "craft_name": self.log.craft_name,
            "duration_s": round(self.log.duration_s, 1),
            "sample_rate_hz": round(self.data.fs, 1),
            "noise": self.noise.to_dict(),
            "step_response": [s.to_dict() for s in self.steps],
            "behaviour": self.behaviour.to_dict(),
            "warnings": self.warnings,
        }


def analyze_log(log: FlightLog) -> LogAnalysis:
    fd = prepare(log)
    warnings: list[str] = []
    if fd.flight_time_s < 10:
        warnings.append(f"only {fd.flight_time_s:.0f}s of flight - results are low confidence "
                        f"(>= 30s with varied throttle and flips/rolls recommended)")
    if fd.fs < 1000:
        warnings.append(f"blackbox sample rate {fd.fs:.0f} Hz is low; noise above {fd.fs / 2:.0f} Hz "
                        "is invisible (set blackbox_sample_rate to 1/2 or higher)")
    noise = analyze_noise(fd)
    steps = analyze_step_responses(fd)
    beh = analyze_behaviour(fd)
    return LogAnalysis(log, fd, noise, steps, beh, warnings)


def pick_best(analyses: list[LogAnalysis]) -> LogAnalysis | None:
    """The longest real flight is the most informative for tuning."""
    flights = [a for a in analyses if a.data.flight_time_s >= 3]
    if not flights:
        return None
    return max(flights, key=lambda a: a.data.flight_time_s)
