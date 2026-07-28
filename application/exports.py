"""报告导出协调：先保存报告，再记录成功或失败 artifact。"""
from __future__ import annotations
from hashlib import sha256
import logging
import os
from pathlib import Path
import subprocess
import sys

from contracts import ExportFormat, ExportStatus, ReportExportArtifact, stable_hash
from presentation.renderers import render_html, render_markdown, render_pdf


logger = logging.getLogger(__name__)

def safe_filename(document):
    raw="_".join((document.report_kind.value,document.market.value,document.instrument.code if document.instrument else "portfolio",document.analysis_mode.value,document.as_of.strftime("%Y%m%dT%H%M%SZ"),document.report_id[:10]))
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in raw)

def export_report(repository, document, *, directory: Path, format: ExportFormat):
    path=directory/f"{safe_filename(document)}.{format.value if format is not ExportFormat.MARKDOWN else 'md'}"
    repository.save_report_document(document)
    temporary=path.with_suffix(path.suffix+".tmp")
    try:
        content={ExportFormat.MARKDOWN:render_markdown,ExportFormat.HTML:render_html,ExportFormat.PDF:render_pdf}[format](document)
        raw=content if isinstance(content,bytes) else content.encode("utf-8")
        content_hash=sha256(raw).hexdigest()
        directory.mkdir(parents=True,exist_ok=True); temporary.write_bytes(raw); temporary.replace(path)
        artifact=ReportExportArtifact(stable_hash({"report":document.report_id,"format":format,"path":str(path),"content":content_hash,"status":ExportStatus.COMPLETED,"error":None,"created":document.generated_at}),document.report_id,format,str(path),content_hash,ExportStatus.COMPLETED,None,document.generated_at)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # The parent itself may be invalid (for example, a file where a
            # directory was requested). Preserve the original export failure.
            pass
        artifact=ReportExportArtifact(stable_hash({"report":document.report_id,"format":format,"path":str(path),"content":None,"status":ExportStatus.FAILED,"error":"REPORT_EXPORT_FAILED","created":document.generated_at}),document.report_id,format,str(path),None,ExportStatus.FAILED,"REPORT_EXPORT_FAILED",document.generated_at)
    repository.save_report_export(artifact); return artifact


def reveal_in_file_manager(path: str | Path) -> bool:
    """Reveal an exported report without making export success depend on the OS shell."""
    target = Path(path).expanduser().resolve()
    try:
        if sys.platform == "darwin":
            command = ("open", "-R", str(target))
        elif os.name == "nt":
            command = ("explorer", "/select,", str(target))
        else:
            command = ("xdg-open", str(target.parent))
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        logger.warning("Unable to reveal exported report path=%s", target, exc_info=True)
        return False


def export_report_and_reveal(repository, document, *, directory: Path, format: ExportFormat):
    artifact = export_report(repository, document, directory=directory, format=format)
    if artifact.status is ExportStatus.COMPLETED:
        reveal_in_file_manager(artifact.path)
    return artifact
