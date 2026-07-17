"""离线发布冒烟：只验证 V2 composition、schema17 和报告 renderer。"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
from tradehelper_v2.config.settings import V2Settings
from tradehelper_v2.contracts import DecisionMode, Exchange, InstrumentId, Market, NewsSnapshot, ReportBlock, ReportBlockKind, ReportDocument, ReportKind, ReportSection, stable_hash
from tradehelper_v2.contracts.presentation import PRESENTATION_POLICY_REF
from tradehelper_v2.presentation.renderers import render_html, render_markdown, render_pdf
from tradehelper_v2.runtime import build_runtime_container
from tradehelper_v2.release.manifest import verify_manifest

def _fixture(now):
    section=ReportSection("smoke","启动检查","确认 renderer 可用",None,(ReportBlock(ReportBlockKind.TEXT,"V2 smoke passed",(PRESENTATION_POLICY_REF,)),))
    identity={"kind":ReportKind.PORTFOLIO,"market":Market.US,"instrument":None,"mode":DecisionMode.EOD,"as_of":now,"title":"TradeHelper smoke","subtitle":"offline","summary":"V2 smoke passed","sections":(section,),"glossary":(),"refs":(PRESENTATION_POLICY_REF,),"schema":1,"renderer":"smoke"}
    return ReportDocument(stable_hash(identity),ReportKind.PORTFOLIO,Market.US,None,DecisionMode.EOD,now,"TradeHelper smoke","offline","V2 smoke passed",(section,),(),(PRESENTATION_POLICY_REF,),1,"smoke",now)

def run_smoke(work_dir: Path | str | None = None) -> dict[str, object]:
    path=Path(work_dir or tempfile.mkdtemp(prefix="tradehelper-v2-smoke-")); settings=V2Settings.from_mapping({"work_dir":str(path)})
    container=build_runtime_container(settings)
    try:
        now=datetime.now(timezone.utc)
        finbert_result=None
        if container.finbert.available:
            news=NewsSnapshot(InstrumentId("AAPL",Market.US,Exchange.UNKNOWN),"TradeHelper smoke test","local",now,now,now,"The company reported steady growth.",False,None,None,None)
            finbert_result=container.finbert.enrich((news,))[0]
        if os.environ.get("TRADEHELPER_REQUIRE_FINBERT") == "1" and getattr(finbert_result,"finbert_label",None) not in {"positive","neutral","negative"}:
            raise RuntimeError("packaged FinBERT inference failed")
        document=_fixture(now); html=render_html(document); markdown=render_markdown(document); pdf=render_pdf(document)
        container.repository.save_report_document(document)
        report_id=document.report_id
    finally: container.close()
    reopened=build_runtime_container(settings)
    try:
        if reopened.repository.get_report_document(report_id) is None:
            raise RuntimeError("packaged SQLite reopen check failed")
        manifest_path=Path(__file__).resolve().parents[2]/"dist_data"/"release-manifest.json"
        manifest=json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
        if os.environ.get("TRADEHELPER_REQUIRE_MANIFEST") == "1" and not manifest:
            raise RuntimeError("release manifest is missing")
        manifest_reasons=verify_manifest(manifest,manifest_path.parents[1]) if manifest else ()
        if os.environ.get("TRADEHELPER_REQUIRE_MANIFEST") == "1" and manifest_reasons:
            raise RuntimeError("release manifest verification failed: " + ",".join(manifest_reasons))
        return {"ok":True,"schema_version":17,"html":len(html),"markdown":len(markdown),"pdf":len(pdf),"finbert_label":getattr(finbert_result,"finbert_label",None),"manifest":manifest,"manifest_reasons":manifest_reasons,"health":reopened.health()}
    finally: reopened.close()
