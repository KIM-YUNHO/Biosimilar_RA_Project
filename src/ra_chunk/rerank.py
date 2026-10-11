"""Cross-encoder re-ranking (bge-reranker-v2-m3): score (query, passage) pairs jointly and
re-order the first-stage candidates. Scores are cached on disk by pair hash."""
from __future__ import annotations

import hashlib
import pickle
from pathlib import Path

MODEL = "BAAI/bge-reranker-v2-m3"


class Reranker:
    def __init__(self, model: str = MODEL, device: str = "cpu", max_length: int = 512,
                 cache: str | Path | None = None, threads: int | None = None):
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        if threads:
            torch.set_num_threads(threads)
        path = Path(model) if Path(model).exists() else Path(snapshot_download(
            model, allow_patterns=["*.json", "model.safetensors", "sentencepiece.bpe.model", "tokenizer*"]))
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForSequenceClassification.from_pretrained(path).to(device).eval()
        self.device = device
        self.max_length = max_length
        self.cache_path = Path(cache) if cache else None
        self.cache: dict[str, float] = {}
        if self.cache_path and self.cache_path.exists():
            self.cache = pickle.loads(self.cache_path.read_bytes())
        self.computed = 0  # pairs scored by the model (not from cache)
        self.seconds = 0.0

    @staticmethod
    def key(query: str, passage: str) -> str:
        return hashlib.sha1(f"{query}\x00{passage}".encode("utf-8")).hexdigest()

    def save(self):
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(self.cache, protocol=pickle.HIGHEST_PROTOCOL))
            tmp.replace(self.cache_path)

    def score(self, query: str, passages: list[str], batch: int = 8) -> list[float]:
        import time

        torch = self.torch
        todo = [p for p in dict.fromkeys(passages) if self.key(query, p) not in self.cache]
        t0 = time.time()
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            enc = self.tok([query] * len(part), part, padding=True, truncation="only_second",
                           max_length=self.max_length, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                logits = self.model(**enc).logits.view(-1).float().cpu().tolist()
            for p, s in zip(part, logits):
                self.cache[self.key(query, p)] = s
        self.computed += len(todo)
        self.seconds += time.time() - t0
        return [self.cache[self.key(query, p)] for p in passages]
