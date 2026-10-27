import csv
import io
import re

import pytest

from tools.samplegen.spec import build_outputs


def read_csv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))


@pytest.fixture(scope="session")
def built() -> dict[str, bytes]:
    return build_outputs()


@pytest.fixture(scope="session")
def spec_rows(built) -> list[dict[str, str]]:
    return read_csv(built["work/spec/spec_rows.csv"])


@pytest.fixture(scope="session")
def respondents(built) -> list[dict[str, str]]:
    return read_csv(built["work/spec/spec_respondents.csv"])


@pytest.fixture(scope="session")
def gt_rows(built) -> list[dict[str, str]]:
    return read_csv(built["ground_truth/rows_gt.csv"])


@pytest.fixture(scope="session")
def batches(built) -> dict[str, str]:
    return {
        k.removeprefix("work/prompts/").removesuffix(".md"): v.decode("utf-8")
        for k, v in built.items()
        if k.startswith("work/prompts/")
    }


def content_rows(rows: list[dict[str, str]], qid: str) -> list[dict[str, str]]:
    return [r for r in rows if r["question"] == qid and r["kind"] == "content"]


ROW_ID_RE = re.compile(r"^### (R\d{4}_Q\d)$", re.MULTILINE)
