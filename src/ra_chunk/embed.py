"""bge-m3 dense + sparse (lexical weight) vectors, computed with transformers directly.

Same computation as FlagEmbedding's BGEM3FlagModel: dense = normalised [CLS] state; sparse =
ReLU(sparse_linear(token states)), max weight per token id, special tokens dropped.
Results are cached on disk by text hash, so re-chunking only embeds new text.
"""
from __future__ import annotations

import hashlib
import pickle
from pathlib import Path

import numpy as np

MODEL = "BAAI/bge-m3"


class BgeM3:
    def __init__(self, model: str = MODEL, device: str = "cpu", max_length: int = 1024,
                 cache: str | Path | None = None, threads: int | None = None, name: str | None = None):
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModel, AutoTokenizer

        if threads:
            torch.set_num_threads(threads)
        path = Path(model) if Path(model).exists() else Path(snapshot_download(
            model, allow_patterns=["*.json", "pytorch_model.bin", "sparse_linear.pt",
                                   "sentencepiece.bpe.model", "tokenizer*"]))
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModel.from_pretrained(path).to(device).eval()
        self.sparse = torch.nn.Linear(self.model.config.hidden_size, 1)
        self.sparse.load_state_dict(torch.load(path / "sparse_linear.pt", map_location="cpu"))
        self.sparse.to(device).eval()
        self.device = device
        self.max_length = max_length
        # recorded with every indexed chunk; vectors of different models are never mixed.
        # Loading from a local directory: pass name="BAAI/bge-m3" to share the index.
        self.name = name or model
        self.special = {self.tok.cls_token_id, self.tok.eos_token_id, self.tok.pad_token_id,
                        self.tok.unk_token_id}
        self.cache_path = Path(cache) if cache else None
        self.cache: dict[str, tuple[np.ndarray, dict[int, float]]] = {}
        if self.cache_path and self.cache_path.exists():
            self.cache = pickle.loads(self.cache_path.read_bytes())

    def save(self):
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps(self.cache, protocol=pickle.HIGHEST_PROTOCOL))
            tmp.replace(self.cache_path)

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def count_tokens(self, text: str) -> int:
        return len(self.tok(text, add_special_tokens=False)["input_ids"])

    def encode(self, texts: list[str], batch_tokens: int = 8192, log=None) -> list[tuple[np.ndarray, dict]]:
        """Embed texts (cached). Batches are formed from texts of similar length."""
        torch = self.torch
        todo = sorted({t for t in texts if self.key(t) not in self.cache},
                      key=lambda t: len(t))
        done = 0
        i = 0
        while i < len(todo):
            # grow the batch while padded size stays under batch_tokens
            longest = min(self.max_length, len(todo[i]) // 3 + 8)
            j = i + 1
            while j < len(todo):
                longest = min(self.max_length, max(longest, len(todo[j]) // 3 + 8))
                if (j - i + 1) * longest > batch_tokens:
                    break
                j += 1
            batch = todo[i:j]
            enc = self.tok(batch, padding=True, truncation=True, max_length=self.max_length,
                           return_tensors="pt").to(self.device)
            with torch.inference_mode():
                h = self.model(**enc).last_hidden_state
                dense = torch.nn.functional.normalize(h[:, 0], dim=-1).float().cpu().numpy()
                w = torch.relu(self.sparse(h)).squeeze(-1).float().cpu().numpy()
            ids = enc["input_ids"].cpu().numpy()
            for k, t in enumerate(batch):
                sp: dict[int, float] = {}
                for tid, wt in zip(ids[k], w[k]):
                    tid = int(tid)
                    if tid in self.special or wt <= 0:
                        continue
                    if wt > sp.get(tid, 0.0):
                        sp[tid] = float(wt)
                self.cache[self.key(t)] = (dense[k].astype(np.float16), sp)
            done += len(batch)
            i = j
            if log and (done // 500) != ((done - len(batch)) // 500):
                log(f"  embedded {done}/{len(todo)}")
                self.save()
        if todo:
            self.save()
        return [self.cache[self.key(t)] for t in texts]


from .search import sparse_score  # noqa: E402,F401  (kept importable from here)
