# bunkoko

部屋の環境（室温・湿度・CO2・明るさなど）と Apple Watch のヘルスケアデータを組み合わせて、睡眠の質を見える化し、改善の提案をするツール。

| 場所 | 中身 |
|---|---|
| [docs/requirements.md](docs/requirements.md) | 要件定義書 |
| [docs/device-selection.md](docs/device-selection.md) | スマートホーム機器の選定と購入リスト |
| [docs/analysis.md](docs/analysis.md) | 分析の設計（使うデータ、1日 = 1行の表、仮説、間違った結論を防ぐ決まり） |
| [docs/phase1-design.md](docs/phase1-design.md) | Phase 1（MVP）の設計（アプリとサーバーの間の決まり、計算のしかた、通知、アプリの構成） |
| [server/](server/) | サーバー（センサーの記録、アプリからのデータの受け取り、1日 = 1行の表の計算、通知）。セットアップ手順は [server/README.md](server/README.md) |
| [ios/](ios/) | iPhone アプリ「健康管理」。ビルドの手順は [ios/README.md](ios/README.md) |
