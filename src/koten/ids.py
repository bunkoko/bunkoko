"""IDの形式（SPEC §7）。"""


def work_id(source: str, source_work_id: str) -> str:
    return f"{source}:{source_work_id}"


def chunk_id(work_id: str, seq: int) -> str:
    return f"{work_id}#{seq}"


def candidate_id(chunk_id: str, seq: int) -> str:
    return f"{chunk_id}/q{seq}"


def episode_id(yyyymmdd: str, seq: int) -> str:
    return f"E{yyyymmdd}-{seq:02d}"
