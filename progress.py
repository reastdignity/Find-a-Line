"""
Progress reporting: two fixed progress bars, one for downloads and one for computations.

In a terminal the bars stay at the bottom and everything printed appears above
them. When the output goes to a file (or another program), there are no bars;
instead a line of text with the progress is printed every few seconds.

Usage:
    from progress import progress
    progress.download.start("NMT tiles", total=40, unit="tile")
    progress.download.update(1, MB=3.2)
    progress.download.finish()
"""

import contextlib
import os
import sys
import time

from tqdm import tqdm

TEXT_INTERVAL = 10  # seconds between progress lines when there is no terminal


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class _Bar:
    """One progress bar, reused for consecutive stages of the same kind."""

    def __init__(self, label: str, position: int, terminal: bool):
        self.label = label
        self.terminal = terminal
        self.total = 0
        self.count = 0
        self.extra = {}
        self.stage = ""
        self.started = self.last_text = 0.0
        self.bar = None
        if terminal:
            self.bar = tqdm(total=0, position=position, leave=True, dynamic_ncols=True,
                            desc=f"{label}: idle", bar_format="{desc}")

    def start(self, stage: str, total: float, unit: str = "it"):
        self.stage, self.total, self.count, self.extra = stage, total, 0, {}
        self.started = self.last_text = time.time()
        if self.bar is not None:
            self.bar.bar_format = None  # default tqdm layout while a stage runs
            self.bar.unit = unit
            self.bar.set_description_str(f"{self.label}: {stage}", refresh=False)
            self.bar.set_postfix_str("", refresh=False)
            self.bar.reset(total=total)

    def update(self, n: float = 1, **extra):
        self.count += n
        self.extra.update(extra)
        if self.bar is not None:
            if self.extra:
                self.bar.set_postfix(self.extra, refresh=False)
            self.bar.update(n)
        elif time.time() - self.last_text >= TEXT_INTERVAL:
            self.last_text = time.time()
            self._print_text()

    def finish(self, note: str = ""):
        if self.bar is not None:
            self.bar.n = self.total
            self.bar.set_description_str(f"{self.label}: {self.stage} - done{note and ', ' + note}",
                                         refresh=False)
            self.bar.refresh()
        else:
            self.count = self.total
            self._print_text(done=True, note=note)

    def _print_text(self, done: bool = False, note: str = ""):
        elapsed = time.time() - self.started
        share = self.count / self.total if self.total else 1
        text = f"[{self.label}] {self.stage}: {self.count:g}/{self.total:g} ({100 * share:.0f}%)"
        text += "".join(f", {k} {v}" for k, v in self.extra.items())
        if done:
            text += f" - done in {_duration(elapsed)}" + (f", {note}" if note else "")
        elif share > 0:
            text += f" - {_duration(elapsed)} elapsed, ~{_duration(elapsed / share * (1 - share))} left"
        print(text, flush=True)

    def close(self):
        if self.bar is not None:
            self.bar.close()


class _Above:
    """stdout replacement that prints whole lines above the progress bars."""

    def __init__(self, stream):
        self.stream = stream
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            tqdm.write(line, file=self.stream)
        return len(text)

    def flush(self):
        if self.buffer:
            tqdm.write(self.buffer, file=self.stream)
            self.buffer = ""
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class Progress:
    def __init__(self):
        self.download = self.compute = None
        self._stdout = sys.stdout

    @contextlib.contextmanager
    def bars(self):
        """Show the bars (in a terminal) for the duration of the block.

        FINDALINE_PROGRESS=bars or =text forces one of the two modes.
        """
        mode = os.environ.get("FINDALINE_PROGRESS", "")
        terminal = mode == "bars" or (mode != "text" and sys.stderr.isatty() and sys.stdout.isatty())
        self.download = _Bar("Download", 0, terminal)
        self.compute = _Bar("Compute", 1, terminal)
        self._stdout = sys.stdout
        try:
            if terminal:
                with contextlib.redirect_stdout(_Above(self._stdout)):
                    yield self
            else:
                yield self
        finally:
            self.download.close()
            self.compute.close()
            self.download = self.compute = _Bar("", 0, False)  # later updates print nothing useful

    def ask(self, question: str) -> str:
        """input() that works while the bars are shown."""
        with tqdm.external_write_mode(file=self._stdout):
            self._stdout.write(question)
            self._stdout.flush()
            return input()


progress = Progress()
# Without progress.bars() (e.g. when a module is used on its own) the bars print text only
progress.download = _Bar("Download", 0, False)
progress.compute = _Bar("Compute", 1, False)
