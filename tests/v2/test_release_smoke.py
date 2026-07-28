from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
from pathlib import Path

from release.manifest import build_manifest, verify_manifest
from release.smoke import _fixture, run_smoke
from presentation.renderers import render_html, render_pdf
from data.migrations.schema import SCHEMA_VERSION


def test_RL60_macos_spec_collects_v2_dependencies_and_excludes_v1():
    source=Path("tradehelper.spec").read_text(encoding="utf-8")
    assert all(f'"{name}"' in source for name in ("runtime","migration"))
    assert "tradehelper_v2" not in source and '"tradehelper"' not in source and "finbert_model" in source
    assert all(f'"{name}"' in source for name in ("alpha","backtest","core","services"))
    assert all(f'"{name}"' not in source for name in ("config","data","strategies","ui"))


def test_RL61_windows_local_and_ci_share_the_same_spec_and_strict_smoke():
    local=Path("scripts/build_windows.bat").read_text(encoding="utf-8-sig")
    ci=Path(".github/workflows/build-windows.yml").read_text(encoding="utf-8")
    for value in (local,ci):
        assert "tradehelper.spec" in value and "TRADEHELPER_REQUIRE_FINBERT" in value and "TRADEHELPER_REQUIRE_MANIFEST" in value


def test_RL62_packaged_entry_has_no_v1_business_imports():
    source=Path("main.py").read_text(encoding="utf-8")
    assert all(f"from {name}" in source for name in ("runtime","data","ui"))
    assert "tradehelper_v2" not in source and "from tradehelper." not in source
    assert all(f"from {name}" not in source for name in ("alpha","backtest","core","report","services"))


def test_RL63_dynamic_runtime_dependencies_are_importable():
    for module in ("jaraco.context","akshare","exchange_calendars","importlib.metadata","scipy._external.array_api_compat.numpy.fft"):
        assert importlib.import_module(module) is not None


def test_RL64_builtin_finbert_loads_and_runs_real_inference(monkeypatch,tmp_path):
    monkeypatch.setenv("TRADEHELPER_REQUIRE_FINBERT","1")
    result=run_smoke(tmp_path)
    assert result["finbert_label"] in {"positive","neutral","negative"}


def test_RL65_packaged_renderers_export_chinese_html_and_pdf():
    document=_fixture(datetime.now(timezone.utc)); document=type(document)(
        document.report_id,document.report_kind,document.market,document.instrument,document.analysis_mode,
        document.as_of,document.title,document.subtitle,document.summary,document.sections,
        document.glossary_entries,document.source_artifact_refs,document.schema_version,
        document.renderer_version,document.generated_at,
    )
    html=render_html(document); pdf=render_pdf(document)
    assert "启动检查" in html and pdf.startswith(b"%PDF")


def test_RL66_temporary_home_smoke_uses_only_the_requested_workdir(tmp_path,monkeypatch):
    home=tmp_path/"home"; work=tmp_path/"work"; home.mkdir(); monkeypatch.setenv("HOME",str(home))
    result=run_smoke(work)
    assert result["ok"] and result["schema_version"] == SCHEMA_VERSION
    assert (work/"tradehelper_v2.db").exists()
    assert not (home/"TradeHelperData").exists()


def test_RL67_legacy_migration_restart_is_covered_by_release_suite():
    source=Path("tests/v2/test_legacy_evidence_isolation.py").read_text(encoding="utf-8")
    assert "test_RL28_completed_migration_is_not_prompted_again_after_restart" in source


def test_RL68_manifest_and_build_files_do_not_contain_configured_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY","release-secret")
    payload=json.dumps(build_manifest(),sort_keys=True)
    build_text="".join(path.read_text(encoding="utf-8",errors="ignore") for path in (Path("tradehelper.spec"),Path("scripts/build_macos.sh"),Path(".github/workflows/build-windows.yml")))
    assert "release-secret" not in payload and "release-secret" not in build_text


def test_RL69_release_manifest_contains_and_verifies_version_commit_dependency_and_model_hash():
    manifest=build_manifest()
    assert not verify_manifest(manifest)
    assert manifest["app_version"]=="2.0.0"
    assert all(manifest[key] for key in ("git_commit","dependency_lock_sha256","finbert_model_sha256"))


def test_web_preview_does_not_auto_open_http_in_https_only_browser():
    source=Path("scripts/run_web_preview.sh").read_text(encoding="utf-8")
    assert "FLET_FORCE_WEB_SERVER=true" in source
    assert "FLET_SERVER_IP=127.0.0.1" in source
    assert "http://localhost:$PORT" in source
    assert "flet run -w" not in source
