"""Contracts package — the single source of truth for every schema.

Import models from here (`from contracts import Test, ClientItem, ...`). The
`REGISTRY` maps a stable schema name to the type whose JSON Schema is exported
to `contracts/jsonschema/` by `contracts.export_jsonschema`.
"""

from __future__ import annotations

from contracts.content import (
    AudioAssetStimulus,
    ClientColourTaskItem,
    ClientGapFillItem,
    ClientImageOption,
    ClientItem,
    ClientMatchingItem,
    ClientOpenWritingItem,
    ClientOption,
    ClientSection,
    ClientSingleChoiceItem,
    ClientTest,
    ClientTextOption,
    ColourTaskItem,
    GapFillItem,
    GappedTextStimulus,
    ImageOption,
    ImageSetStimulus,
    Item,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    Option,
    PassageTextStimulus,
    PoolOption,
    Section,
    SingleChoiceItem,
    Stimulus,
    Test,
    TextOption,
)
from contracts.runtime import (
    AnalysisVerdict,
    Answer,
    Attempt,
    AttemptReport,
    AudioTrackProgress,
    GradingResult,
    HiddenInterval,
    IntegrityEvent,
    IntegrityProfile,
    ItemGrade,
    ManualGrade,
    QuestionTiming,
    RosterEntry,
    RuleEventRef,
    RuleEvidence,
    RuleHit,
    RuleSeverity,
    SkillReport,
    WritingAssessment,
    WritingFeatures,
)

__all__ = [
    # content (authoring)
    "Test",
    "Section",
    "Item",
    "Option",
    "TextOption",
    "ImageOption",
    "SingleChoiceItem",
    "GapFillItem",
    "MatchingItem",
    "OpenWritingItem",
    "ColourTaskItem",
    "Stimulus",
    "PassageTextStimulus",
    "AudioAssetStimulus",
    "ImageSetStimulus",
    "GappedTextStimulus",
    "MatchingPoolStimulus",
    "PoolOption",
    # content (client / student-facing)
    "ClientTest",
    "ClientSection",
    "ClientItem",
    "ClientOption",
    "ClientTextOption",
    "ClientImageOption",
    "ClientSingleChoiceItem",
    "ClientGapFillItem",
    "ClientMatchingItem",
    "ClientOpenWritingItem",
    "ClientColourTaskItem",
    # runtime
    "Attempt",
    "AudioTrackProgress",
    "Answer",
    "ManualGrade",
    "IntegrityEvent",
    "GradingResult",
    "ItemGrade",
    "IntegrityProfile",
    "QuestionTiming",
    "HiddenInterval",
    "WritingFeatures",
    "RuleHit",
    "RuleEvidence",
    "RuleEventRef",
    "RuleSeverity",
    "AnalysisVerdict",
    "WritingAssessment",
    "AttemptReport",
    "SkillReport",
    "RosterEntry",
    # client-facing schema names (used by the no-`correct` invariant test)
    "CLIENT_FACING",
    "REGISTRY",
]

# Schemas that are served to the student's browser. The grading-invariant test
# (CLAUDE.md #1) asserts none of these can carry an answer-key field.
CLIENT_FACING: tuple[str, ...] = (
    "ClientTest",
    "ClientSection",
    "ClientItem",
)

# name -> model/union exported to contracts/jsonschema/<name>.json
REGISTRY: dict[str, object] = {
    # authoring content
    "Test": Test,
    "Section": Section,
    "Item": Item,
    "Option": Option,
    "Stimulus": Stimulus,
    # client content
    "ClientTest": ClientTest,
    "ClientSection": ClientSection,
    "ClientItem": ClientItem,
    "ClientOption": ClientOption,
    # runtime
    "Attempt": Attempt,
    "AudioTrackProgress": AudioTrackProgress,
    "Answer": Answer,
    "ManualGrade": ManualGrade,
    "IntegrityEvent": IntegrityEvent,
    "GradingResult": GradingResult,
    "IntegrityProfile": IntegrityProfile,
    "WritingFeatures": WritingFeatures,
    "RuleHit": RuleHit,
    "AnalysisVerdict": AnalysisVerdict,
    "WritingAssessment": WritingAssessment,
    "AttemptReport": AttemptReport,
    "SkillReport": SkillReport,
    "RosterEntry": RosterEntry,
}
