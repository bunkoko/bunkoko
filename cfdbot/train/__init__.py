"""複数銘柄のポートフォリオ学習（ウォークフォワード + リスク配分 + 分散実行）。"""

from .config import TrainConfig, load_train_config

__all__ = ["TrainConfig", "load_train_config"]
