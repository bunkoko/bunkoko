"""学習結果ではない構成（config/baselines/*.json など）から、EA 用ファイルを作る。

    python scripts/make_ea.py --final config/baselines/turtle55_d1.json
    → output/live/turtle55_d1/ に ea/*.set・ea/*.txt と final.json（学習結果と同じ形）を作る

1 回の損失・合計の上限・資金は config（./cfd risk で保存した値）を使う。できたフォルダは
python scripts/mt5_files.py install --run output/live/<名前> で MT5 に置ける（./cfd preset は両方を続けて行う）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfdbot.export import export_sleeve  # noqa: E402
from cfdbot.train.config import load_train_config, tf_minutes  # noqa: E402
from cfdbot.train.dataset import load_dataset  # noqa: E402
from cfdbot.train.evaluate import make_sleeve, pick_task  # noqa: E402
from cfdbot.train.pipeline import _account_config, _eval_settings  # noqa: E402
from cfdbot.train.portfolio import Pick  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--final", required=True, help="構成の JSON")
    p.add_argument("--out", help="出力先（省略時は output/live/<JSON の名前>）")
    p.add_argument("--config", default="config/train.toml")
    args = p.parse_args()

    src = Path(args.final)
    out = Path(args.out) if args.out else Path("output/live") / src.stem
    cfg = load_train_config(args.config)
    conf = json.loads(src.read_text(encoding="utf-8"))
    ds = load_dataset(cfg.data)
    settings = _eval_settings(cfg, ds)
    fx = ds.fx if ds.fx is not None else cfg.data.fx
    bt_cfg = _account_config(cfg, fx, settings.event_list())
    instruments = settings.instrument_map()
    acc = cfg.account

    ea_dir = out / "ea"
    if ea_dir.exists():  # 前の版のファイル（銘柄・時間足が変わった分）を残さない
        for f in ea_dir.glob("cfdbot_*"):
            f.unlink()
    rows = []
    for s in conf["sleeves"]:
        pick = Pick(s["symbol"], s["strategy"], s["params"], s.get("exit", {}), 0, 0.0, 0.0, 0,
                    float(s.get("multiplier", 1.0)), s["timeframe"], s.get("htf") or {})
        sleeve = make_sleeve(pick_task(pick), ds, risk_weight=pick.multiplier)
        paths = export_sleeve(sleeve, instruments[pick.symbol], bt_cfg, ea_dir, tf_minutes(pick.timeframe),
                              suffix=pick.timeframe)
        rows.append({**s, "risk_per_trade": round(acc.base_risk * pick.multiplier, 5),
                     "fill_timeframe": ds.fine_timeframe(pick.symbol, pick.timeframe),
                     "ea_files": [str(x.relative_to(out)) for x in paths]})
    final = {"source": str(src), "note": conf.get("note", ""), "equity": acc.equity,
             "max_total_risk": acc.max_total_risk, "cluster_max_risk": acc.cluster_max_risk,
             "signal_timeframes": sorted({s["timeframe"] for s in conf["sleeves"]}), "sleeves": rows}
    out.mkdir(parents=True, exist_ok=True)
    (out / "final.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EA 用ファイルを作った: {out}/ea/（資金 {acc.equity:,.0f} 円・同時に持てる合計 {acc.max_total_risk:.1%}）")
    for r in rows:
        print(f"  {r['symbol']:<7}{r['strategy']} @{r['timeframe']}  1回の損失 {r['risk_per_trade']:.2%}")


if __name__ == "__main__":
    main()
