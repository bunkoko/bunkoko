"""銘柄選定 AI: 値動きだけから、全銘柄に「これから 5 日の順位」の予測を付ける（docs/stocks_short.md の M2）。"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .. import stocks_short as H


def scores(open_: pd.DataFrame, close: pd.DataFrame, log: Callable[[str], None] = print) -> pd.DataFrame:
    """2010 年から毎年、その年より前だけで学び直した予測（日 × 株。予測の無い所は NaN）。"""
    _, x = H.features(open_, close)
    _, y5 = H.targets(open_, close)
    tradable = (close.notna() & H.parts(open_, close)["night"].notna()).to_numpy()
    return H.walk_forward(x, y5, tradable, embargo=H.EMBARGO, log=log)


def scores_extra(open_: pd.DataFrame, close: pd.DataFrame, extra: dict[str, pd.DataFrame], first_year: int = H.FIRST_TEST_YEAR,
                 log: Callable[[str], None] = print) -> pd.DataFrame:
    """値動きの 24 個に、extra（名前 → 日 × 株。その日の 13:30 までに分かる値）をその日の順位にして足した版（docs/agents.md 11 章）。

    学び方は scores と同じ。first_year はどの年から予測するか（入力が途中からしか無いときは、値のたまった年から）。
    """
    _, x = H.features(open_, close)
    add = [H.rank_pct(extra[k].reindex(index=close.index, columns=close.columns)).to_numpy(dtype=np.float32) for k in extra]
    x = np.concatenate([x, np.stack(add, axis=2)], axis=2)
    _, y5 = H.targets(open_, close)
    tradable = (close.notna() & H.parts(open_, close)["night"].notna()).to_numpy()
    return H.walk_forward(x, y5, tradable, first_year=first_year, embargo=H.EMBARGO, log=log)
