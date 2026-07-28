"""V2-10 受控研究入口；不被确定性交易主链反向导入。"""

from .context import ResearchContextBuilder
from .parser import StrictHypothesisParser
from .validator import DeterministicHypothesisValidator
from .bridge import CandidateBridge
from .engine import ResearchEngine
from .registry import ResearchMappingRegistry
from .prompt import build_prompt, build_prompt_chunks

__all__ = ["CandidateBridge", "DeterministicHypothesisValidator", "ResearchContextBuilder", "ResearchEngine", "ResearchMappingRegistry", "StrictHypothesisParser", "build_prompt", "build_prompt_chunks"]
