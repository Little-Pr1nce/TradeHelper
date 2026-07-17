"""可复现发布 manifest，不携带 token 或用户账户。"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import platform
from pathlib import Path
import subprocess
from tradehelper_v2.runtime.version import APP_VERSION


def _model_hash(model: Path) -> str | None:
    digest=hashlib.sha256()
    files=tuple(sorted(item for item in model.rglob("*") if item.is_file())) if model.exists() else ()
    for item in files:
        digest.update(str(item.relative_to(model)).encode())
        digest.update(item.read_bytes())
    return digest.hexdigest() if files else None

def build_manifest(root: Path | str = ".") -> dict[str, object]:
    root=Path(root); lock=root/"requirements-lock.txt"; lock_hash=hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None
    try: commit=subprocess.check_output(("git","rev-parse","HEAD"),cwd=root,text=True,stderr=subprocess.DEVNULL).strip()
    except Exception: commit="unknown"
    model=root/"dist_data"/"finbert_model"
    return {"app_version":APP_VERSION,"git_commit":commit,"python":platform.python_version(),"platform":platform.platform(),"dependency_lock_sha256":lock_hash,"finbert_model_sha256":_model_hash(model),"built_at":datetime.now(timezone.utc).isoformat()}


def verify_manifest(manifest: dict[str, object], root: Path | str = ".") -> tuple[str, ...]:
    """Return stable reason codes for missing or mismatched packaged assets."""
    root=Path(root); reasons=[]
    required=("app_version","git_commit","python","platform","dependency_lock_sha256","finbert_model_sha256","built_at")
    if any(not manifest.get(key) for key in required): reasons.append("RELEASE_MANIFEST_INCOMPLETE")
    if manifest.get("app_version")!=APP_VERSION: reasons.append("RELEASE_VERSION_MISMATCH")
    lock=root/"requirements-lock.txt"
    if lock.exists() and manifest.get("dependency_lock_sha256")!=hashlib.sha256(lock.read_bytes()).hexdigest():
        reasons.append("RELEASE_DEPENDENCY_HASH_MISMATCH")
    model=root/"dist_data"/"finbert_model"
    model_hash=_model_hash(model)
    if model_hash is not None and manifest.get("finbert_model_sha256")!=model_hash:
        reasons.append("RELEASE_MODEL_HASH_MISMATCH")
    return tuple(sorted(set(reasons)))
