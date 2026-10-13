"""銘柄選定 AI: 値動きだけから、全銘柄に「これから 5 日の順位」の予測を付ける（docs/stocks_short.md の M2）。"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from .. import stocks_short as H


def scores(open_: pd.DataFrame, close: pd.DataFrame, log: Callable[[str], None] = print) -> pd.DataFrame:
    """2010 年から毎年、その年より前だけで学び直した予測（日 × 株。予測の無い所は NaN）。"""
    _, x = H.features(open_, close)
    _, y5 = H.targets(open_, close)
    tradable = (close.notna() & H.parts(open_, close)["night"].notna()).to_numpy()
    return H.walk_forward(x, y5, tradable, embargo=H.EMBARGO, log=log)
