"""n8n から呼ぶローカルAPI（SPEC §11。127.0.0.1 だけで待ち受ける）。

起動時に SSD の索引と候補表をメモリへ読み込み、HDD には触れない（SPEC §5）。
M2 では /healthz だけ。回づくりのエンドポイントは M4 以降で足す。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response

from koten.index.search import HotIndex


def create_app(ssd_root: Path, embed_model: str) -> FastAPI:
    """HDD のパスは受け取らない。SSD の場所と埋め込みモデル名だけで起動する。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            app.state.index = HotIndex(ssd_root, embed_model)
            app.state.index_error = None
        except FileNotFoundError as e:
            app.state.index = None
            app.state.index_error = str(e)
        yield

    app = FastAPI(title="koten", lifespan=lifespan)

    @app.get("/healthz")
    def healthz(response: Response) -> dict:
        index: HotIndex | None = app.state.index
        if index is None:
            # n8n が状態コードだけで判断できるよう、索引が無ければ 503 にする
            response.status_code = 503
            return {"status": "no_index", "error": app.state.index_error}
        return {
            "status": "ok",
            "index": {
                "model": index.meta["model"],
                "candidates": len(index),
                "tokenizer": index.meta["tokenizer"],
                "built_at": index.meta["built_at"],
            },
        }

    return app
