"""確認画面と、人の承認・差し戻し（SPEC §8.12・docs/m6_design.md の koten.review）。

確認画面（review.html）は読むだけで、DB は変えない。承認・差し戻し・人の一言は、
画面に出したコマンド（koten episode approve・reject・take）が actions の関数を呼んで変える。
"""
