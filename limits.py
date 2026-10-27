"""
Limits of the web app, from environment variables (pydantic-settings).

Each field is read from FINDALINE_LIMIT_<NAME> (e.g. FINDALINE_LIMIT_COMPUTE_SECONDS=7200).
Nothing set means no limit: the local app (start.bat, `findaline.py web`) runs as before;
the public site sets them for its worker. For now these are the time limits of the
worker (jobs.Scheduler) and how long its housekeeping keeps old events; the limits on
request values come with the public mode.
"""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Limits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINDALINE_LIMIT_", extra="ignore")

    download_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of the download phase of a job (its processes are killed, the "
                                "job fails with \"time limit\")")
    compute_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of the computing phase of a job")
    quick_seconds: Optional[float] = Field(
        None, gt=0, description="time limit of an estimate or a profile (the quick lane's process is killed "
                                "and started again)")
    keep_events_days: Optional[float] = Field(
        90, gt=0, description="days the events (messages, progress) of a finished job are kept; the job "
                              "itself stays in the queue")

    def phase_seconds(self, phase: str) -> Optional[float]:
        """The time limit of a phase of a job ("download" or "compute")."""
        return {"download": self.download_seconds, "compute": self.compute_seconds}[phase]
