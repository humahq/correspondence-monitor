"""Notes to the admins about messages that try to trick the bot (CAP-09): the ready-made detector below, and
messages the reader found giving the bot orders (step 4, `orders_flagged`).

A ready-made trick detector (CAP-09, layer 1): Meta's open-source Llama Prompt Guard 2 (22M), run on our own
machine with onnxruntime, so no message text leaves it. It only adds: a flagged message is recorded in the audit
log and the admins are told. It never clears, hides or changes an alert, and a detector that fails or is missing
never stops a message from being read.

Our own layers come after it: a claim of permission is not accepted (`features.permission_claims`) and a second
look reads only the facts (`features.second_look`). Measured on our test sets (5 Nov, `demo/trick-detector`): it
flags 4 of 89 trick messages and 0 of 1,220 normal ones. It catches the blunt kind ("ignore your instructions"),
not "the sponsor said it's fine".

The model folder is made by `deploy/prompt_guard_export.py`; its two files must match SHA256 below, so a swapped
model is refused. About 230 MB of memory, about 50 ms a message.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from . import db, settings

MODEL = "Llama Prompt Guard 2 (22M), Meta"
MODEL_DIR = "models/prompt-guard-2-22m"
SHA256 = "0349fe0bcf583d42d162eaa693a92a305f7d211f4abe8f69e1fd00903cc35308"
THRESHOLD = 0.9   # at 0.5 a normal note ("caregiver gives via tube each AM per pharmacy instructions") scored 0.77
PIECE, STEP = 510, 400   # tokens read at a time (the model reads 512 with its two markers), and the overlap

_lock = threading.Lock()
_loaded: dict = {}


def on() -> bool:
    return settings.feature("trick_detector")


def _load() -> dict:
    with _lock:
        if not _loaded:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
            ort.disable_telemetry_events()   # no usage events to Microsoft from a machine holding trial data
            folder = Path(settings.get("trick_detector.model_dir", MODEL_DIR))
            folder = folder if folder.is_absolute() else Path(__file__).resolve().parents[1] / folder
            model = folder / "model.onnx"
            digest = hashlib.sha256()
            for part in (model, folder / "model.onnx.data"):
                with part.open("rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(block)
            if digest.hexdigest() != SHA256:
                raise RuntimeError(f"{model} is not the approved model (sha256 {digest.hexdigest()[:12]}...)")
            tok = Tokenizer.from_file(str(folder / "tokenizer.json"))
            tok.no_truncation()
            tok.no_padding()
            labels = json.loads((folder / "config.json").read_text()).get("id2label") or {"0": "LABEL_0", "1": "LABEL_1"}
            bad = int(next(i for i, label in labels.items() if label.upper() in ("MALICIOUS", "INJECTION", "LABEL_1")))
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.enable_mem_pattern = False
            _loaded.update(np=np, tok=tok, bad=bad, cls=tok.token_to_id("[CLS]"), sep=tok.token_to_id("[SEP]"),
                           sess=ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"]))
        return _loaded


def score(text: str) -> float:
    """How sure the model is that the text tries to give the AI orders, 0 to 1. A long text is read in overlapping
    pieces; the highest piece counts."""
    m = _load()
    np = m["np"]
    ids = m["tok"].encode(text, add_special_tokens=False).ids
    best = 0.0
    for i in range(0, max(len(ids), 1), STEP):
        piece = np.array([[m["cls"]] + ids[i:i + PIECE] + [m["sep"]]], dtype=np.int64)
        logits = m["sess"].run(None, {"input_ids": piece, "attention_mask": np.ones_like(piece)})[0][0]
        e = np.exp(logits - logits.max())
        best = max(best, float(e[m["bad"]] / e.sum()))
        if i + PIECE >= len(ids):
            break
    return best


def _tell_admins(key: str, study_id: str, msg_id: int, what: str) -> None:
    from .compliance import alerts
    base = settings.get("web.base_url", "").rstrip("/")
    link = f"\nRead it: {base}/admin/messages/{msg_id}\n" if base else ""
    alerts.notify(f"{key}:{msg_id}", f"[{study_id}] A message may be trying to trick the bot",
                  f"Message {msg_id} in study {study_id} {what}.\n"
                  "The bot followed none of it and changed nothing else because of this note. Please read it, and "
                  f"tell the CRA if it looks deliberate.\n{link}", every_minutes=24 * 60)


def orders_flagged(study_id: str, msg_id: int, orders: list[dict]) -> None:
    """CAP-09 step 4: a message that gives the bot orders is recorded and the admins are told (without its text).
    The reader has already made sure it gets no quiet "no problem"."""
    db.audit("orders_to_bot_flagged", entity_type="message", entity_id=msg_id, study_id=study_id,
             orders=len(orders), wants=sorted({o["wants"] for o in orders}))
    _tell_admins("orders", study_id, msg_id, f"gives the bot orders about how to check or report it "
                 f"({len(orders)} sentence(s); it was sent as \"please check\")")


def check(study_id: str, msg_id: int, text: str) -> float | None:
    """Score a message; when flagged, record it and tell the admins (no message text in the note). The score, or
    None when the switch is off or the detector could not run."""
    if not on() or not text.strip():
        return None
    threshold = float(settings.get("trick_detector.threshold", THRESHOLD))
    try:
        s = score(text)
    except Exception as e:
        db.audit("trick_detector_failed", entity_type="message", entity_id=msg_id, study_id=study_id,
                 model=MODEL, error=str(e)[:300])
        return None
    if s < threshold:
        return s
    db.audit("trick_detector_flagged", entity_type="message", entity_id=msg_id, study_id=study_id,
             model=MODEL, score=round(s, 3), threshold=threshold)
    _tell_admins("trick", study_id, msg_id, f"looks like it tries to give the AI orders ({MODEL}, score {s:.2f})")
    return s
