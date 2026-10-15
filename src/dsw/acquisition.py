"""Host acquisition targets; stopping occurs after the current UART operation."""
from dataclasses import dataclass


@dataclass
class AcquisitionPreset:
    mode: str = 'manual'
    target: float = 60.
    channel: int = 1
    baseline_counts: dict | None = None
    started: float | None = None
    stop_requested: bool = False

    def arm(self, now, counts):
        if self.mode not in ('manual', 'duration', 'counts') or self.target <= 0:
            raise ValueError('Invalid acquisition preset')
        self.started, self.baseline_counts, self.stop_requested = now, dict(counts), False

    def progress(self, now, counts):
        if self.started is None:
            return 0., False, 'No active preset'
        elapsed = max(0., now-self.started)
        if self.mode == 'duration':
            value = elapsed
            message = f'{elapsed:.1f} / {self.target:g} s'
        elif self.mode == 'counts':
            channels = (1, 2) if self.channel == 0 else (self.channel,)
            value = sum(max(0, counts.get(ch, 0)-self.baseline_counts.get(ch, 0)) for ch in channels)
            message = f'{value:,} / {int(self.target):,} counts'
        else:
            return 0., False, f'Elapsed {elapsed:.1f} s'
        reached = value >= self.target
        return min(1., value/self.target), reached and not self.stop_requested, message
