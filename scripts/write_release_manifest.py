from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tradehelper_v2.release.manifest import build_manifest


target=Path("dist_data/release-manifest.json")
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(build_manifest(),ensure_ascii=False,indent=2),encoding="utf-8")
