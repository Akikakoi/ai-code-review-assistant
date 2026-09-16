"""L3 相似实现的向量检索（ADR 0006 的小仓库策略）。

## 为什么索引的是"确定性文本"而不是"自然语言摘要"

ADR 0006 写的是"每个方法的自然语言摘要（轻量模型预生成）+ 方法签名"。
v1 改为嵌入 **签名 + 方法源码** 的确定性拼接 —— 三个理由：

1. 摘要要按方法数计费，而索引要覆盖全仓库；成本在验证阈值合理性**之前**就发生，
   顺序反了；
2. 确定性文本可重建、可增量（源码没变就不重嵌），摘要不行；
3. bge/text-embedding-v2 都是通用文本模型，对代码的语义区分本来就有限 ——
   先用确定性文本验证 0.75 这个阈值是否成立，再决定要不要上摘要。

这个偏离记在 ADR 0006 补记三里；若阈值验证通过、需要更强的语义匹配，
摘要版本是升级路径而不是推翻。

## 嵌入后端

按可用性自动选择，**都不可用时如实报告**而不是静默退化为符号匹配：

| 后端 | 条件 | 说明 |
| --- | --- | --- |
| `local_bge` | venv 里有 sentence-transformers 且模型可用 | 本地推理，零调用成本；首次加载慢 |
| `http` | 配置了 `acra_l3_embedding_endpoint` | 复用外部嵌入服务（如 stellar-mall rag-backend 的 `POST /embed`） |

BGE 向量在入库前做了归一化，余弦相似度因此退化为点积。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from acra.models import CodeSlice, FileDiff, SymbolSpan
from acra.repo.symbol_index import find_enclosing_spans, language_for_path

#: ADR 0006：相似度门槛（两条策略共用同一语义）
SIMILARITY_THRESHOLD = 0.75
#: top-K（ADR 0006）
TOP_K_SIMILAR = 3
#: 一次批量嵌入的方法数上限（控制单次请求/推理的体量）
EMBED_BATCH_SIZE = 32
#: 单个方法的嵌入文本上限（字符）——超长方法截断，索引不需要全文语义
MAX_METHOD_TEXT = 1200


class EmbeddingBackend(Protocol):
    """嵌入后端协议：批量文本 → 批量归一化向量。"""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalBgeBackend:
    """本地 BGE（sentence-transformers）。依赖缺失时 `available=False`。"""

    def __init__(self, model_name: str = "BAAI/bge-large-zh-v1.5", local_path: str = "") -> None:
        self.model_name = local_path or model_name
        self._model = None
        self._load_error: str | None = None

    @property
    def name(self) -> str:
        return f"local_bge:{self.model_name}"

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if self._load_error:
            return False
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
            return True
        except Exception as exc:  # noqa: BLE001 - 依赖缺失/模型不可用都是同一类"没就位"
            self._load_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return False

    def available(self) -> tuple[bool, str]:
        if self._ensure_model():
            return True, ""
        return False, self._load_error or "模型未加载"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._ensure_model():
            raise RuntimeError(self._load_error)
        vectors = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vectors]


class HttpEmbedBackend:
    """外部嵌入服务的 HTTP 后端（stellar-mall rag-backend 的 `POST /embed` 形状）。

    请求：`{"texts": [...]}`；响应：`{"embeddings": [[...]], "dim": N}`。
    """

    def __init__(self, endpoint: str, timeout: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        return f"http:{self.endpoint}"

    def available(self) -> tuple[bool, str]:
        try:
            vecs = self.embed(["ping"])
            return (len(vecs) == 1 and len(vecs[0]) > 0), ""
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return False, self._last_error or "服务不可达"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(f"{self.endpoint}/embed", json={"texts": texts}, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise ValueError(f"嵌入服务返回 {len(vectors)} 条，期望 {len(texts)} 条")
        return vectors


class OpenAICompatBackend:
    """OpenAI 兼容 `/embeddings` 后端（DashScope 等云端）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"openai_compatible:{self.model}"

    def available(self) -> tuple[bool, str]:
        try:
            vecs = self.embed(["ping"])
            return (len(vecs) == 1 and len(vecs[0]) > 0), ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:160]}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = sorted(resp.json().get("data", []), key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in data]


def pick_backend(settings) -> tuple[EmbeddingBackend | None, str]:
    """按配置选嵌入后端。返回 (backend 或 None, 说明)。"""
    endpoint = getattr(settings, "acra_l3_embedding_endpoint", "")
    if endpoint:
        return HttpEmbedBackend(endpoint), ""
    local = LocalBgeBackend(
        model_name=getattr(settings, "acra_l3_embedding_model", "BAAI/bge-large-zh-v1.5"),
        local_path=getattr(settings, "acra_l3_embedding_local_path", ""),
    )
    ok, why = local.available()
    if ok:
        return local, ""
    return None, f"本地 BGE 不可用：{why}"


def method_text(span: SymbolSpan) -> str:
    """方法的嵌入文本：签名 + 源码前缀（确定性，可重建）。"""
    head = span.signature or f"{span.kind} {span.name}"
    body = span.source[:MAX_METHOD_TEXT]
    return f"{head}\n{body}"[: MAX_METHOD_TEXT + 200]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class VectorHit:
    path: str
    start_line: int
    end_line: int
    name: str
    score: float
    source: str


class MethodVectorIndex:
    """方法级向量索引：SQLite 落盘 + content-hash 增量 + 暴力余弦。

    为什么不用 ChromaDB：acra 的规模是"一个仓库的方法数"（几千到几万），
    归一化后的余弦用 numpy 是毫秒级暴力扫描 —— 引入一个向量数据库
    换不来任何这个量级上的收益，反而多一份部署依赖。
    """

    def __init__(self, db_path: Path, backend: EmbeddingBackend, dim: int | None = None) -> None:
        self.db_path = Path(db_path)
        self.backend = backend
        self.dim = dim
        self._conn: sqlite3.Connection | None = None
        self.embedded_count = 0

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            conn.execute(
                """create table if not exists method (
                        path text, start_line int, end_line int, name text,
                        text text, hash text, dim int, vec blob,
                        primary key (path, start_line))"""
            )
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---------------------------------------------------------------- 构建

    def build(
        self,
        files: list[str],
        read_lines: Callable[[str], list[str]],
        *,
        max_files: int = 2000,
    ) -> tuple[int, list[str]]:
        """为仓库文件建立/增量更新索引。返回 (本次嵌入的方法数, 说明)。"""
        notes: list[str] = []
        conn = self._connect()
        targets = [
            p for p in files if p and language_for_path(p)
        ][:max_files]
        if len(files) > max_files:
            notes.append(f"向量索引按配额只覆盖 {max_files} 个文件")

        # 1. 收集待嵌入的方法（hash 未变的直接跳过）
        pending: list[tuple[tuple, str, SymbolSpan, str]] = []
        for path in targets:
            lines = read_lines(path)
            if not lines:
                continue
            hot = [i for i, line in enumerate(lines, 1) if line.strip()]
            if not hot:
                continue
            spans, _ = find_enclosing_spans("\n".join(lines), set(hot), path=path)
            for span in spans:
                if span.kind not in ("method", "function", "constructor"):
                    continue
                text = method_text(span)
                digest = text_hash(text)
                row = conn.execute(
                    "select hash from method where path=? and start_line=?",
                    (path, span.start_line),
                ).fetchone()
                if row and row[0] == digest and row[0]:
                    continue
                pending.append(((path, span.start_line), text, span, digest))

        # 2. 批量嵌入
        for offset in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[offset : offset + EMBED_BATCH_SIZE]
            vectors = self.backend.embed([text for _, text, _, _ in batch])
            for (key, text, span, digest), vec in zip(batch, vectors, strict=True):
                conn.execute(
                    "insert or replace into method "
                    "(path, start_line, end_line, name, text, hash, dim, vec) "
                    "values (?,?,?,?,?,?,?,?)",
                    (
                        key[0], key[1], span.end_line, span.name, text, digest,
                        len(vec), _pack(vec),
                    ),
                )
                self.embedded_count += 1
        conn.commit()
        return len(pending), notes

    # ---------------------------------------------------------------- 查询

    def query(
        self, file_diff: FileDiff, spans: list[SymbolSpan], *, top_k: int = TOP_K_SIMILAR
    ) -> list[VectorHit]:
        """以变更方法的文本为 query，取相似度最高且不属于自身的 top-K。"""
        queries = [(span, method_text(span)) for span in spans]
        queries = [(span, text) for span, text in queries if text.strip()]
        if not queries:
            return []
        query_vecs = self.backend.embed([text for _, text in queries])

        conn = self._connect()
        rows = conn.execute(
            "select path, start_line, end_line, name, text, vec from method where vec is not null"
        ).fetchall()
        if not rows:
            return []

        hits: dict[tuple[str, int], VectorHit] = {}
        for qv in query_vecs:
            for path, start_line, end_line, name, text, blob in rows:
                if path == file_diff.path:
                    continue  # 过滤掉自己（ADR 0006）
                score = _cosine(qv, _unpack(blob))
                if score < SIMILARITY_THRESHOLD:
                    continue
                key = (path, start_line)
                best = hits.get(key)
                if best is None or score > best.score:
                    hits[key] = VectorHit(
                        path=path,
                        start_line=int(start_line),
                        end_line=int(end_line),
                        name=str(name),
                        score=round(score, 4),
                        source=text.split("\n", 1)[-1][:MAX_METHOD_TEXT],
                    )
        ranked = sorted(hits.values(), key=lambda h: -h.score)[:top_k]
        return [
            CodeSlice(
                path=hit.path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                source=hit.source,
                score=hit.score,
            )
            for hit in ranked
        ]


# ---------------------------------------------------------------------------- 序列化


def _pack(vec: list[float]) -> bytes:
    return json.dumps(vec, separators=(",", ":")).encode("utf-8")


def _unpack(blob) -> list[float]:
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    return json.loads(blob)


def _cosine(a: list[float], b: list[float]) -> float:
    """归一化向量的余弦 = 点积。缺 numpy 时用纯 Python，量级在毫秒到秒之间。"""
    try:
        import numpy as np

        va, vb = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
        denom = float((float((va * va).sum()) * float((vb * vb).sum())) ** 0.5)
        return float((va * vb).sum()) / denom if denom else 0.0
    except ImportError:
        num = sum(x * y for x, y in zip(a, b, strict=False))
        da = sum(x * x for x in a) ** 0.5
        db = sum(y * y for y in b) ** 0.5
        return num / (da * db) if da and db else 0.0
