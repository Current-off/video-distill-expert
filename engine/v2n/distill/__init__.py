"""Video distillation: one intermediate representation, many output formats.

Public surface:

* :func:`build_ir` — cleaned segments -> traceable claims and topics
* :func:`load_ir` / :func:`write_ir` — persist and re-read the IR
* :func:`render_all` — project the IR into the formats the user picked

Nothing here writes to the transcript, the notes or the pipeline state; the
module is a consumer of ``clean`` output and a producer of its own artefacts.
"""
from __future__ import annotations

from .agent_backend import AgentDistiller
from .backends import Distiller, LLMDistiller, RulesDistiller, distil, make_distiller
from .ir import build_ir, is_traceable, load_ir, write_ir
from .model import SCHEMA_VERSION, Claim, DistillIR, Topic
from .renderers import RENDERERS, available, render_all, resolve

__all__ = [
    "RENDERERS",
    "SCHEMA_VERSION",
    "AgentDistiller",
    "Claim",
    "DistillIR",
    "Distiller",
    "LLMDistiller",
    "RulesDistiller",
    "Topic",
    "available",
    "build_ir",
    "distil",
    "is_traceable",
    "load_ir",
    "make_distiller",
    "render_all",
    "resolve",
    "write_ir",
]
