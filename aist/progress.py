"""Progress of a pipeline's long-running stages, kept in Redis while they run."""

from dataclasses import dataclass


def progress_percent(done: int, total: int) -> int:
    """Whole percent of ``total`` that is ``done``; nothing to do counts as complete."""
    return 100 if total == 0 else int(done * 100 / total)


@dataclass(frozen=True)
class EnrichProgress:

    """How many of a pipeline's findings the enrich chord has finished."""

    total: int
    done: int

    @staticmethod
    def key(pipeline_id: str) -> str:
        return f"aist:progress:{pipeline_id}:enrich"

    @classmethod
    def start(cls, client, pipeline_id: str, total: int) -> None:
        client.hset(cls.key(pipeline_id), mapping={"total": total, "done": 0})

    @classmethod
    def advance(cls, client, pipeline_id: str, findings: int) -> None:
        client.hincrby(cls.key(pipeline_id), "done", findings)

    @classmethod
    def read(cls, client, pipeline_id: str) -> "EnrichProgress | None":
        """Return the pipeline's progress, or None while its enrich chord has not started."""
        total, done = client.hmget(cls.key(pipeline_id), "total", "done")
        if total is None:
            return None
        return cls(total=int(total), done=int(done or 0))

    @property
    def percent(self) -> int:
        return progress_percent(self.done, self.total)

    @property
    def finished(self) -> bool:
        return self.done >= self.total
