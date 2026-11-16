"""Explicit Design Arena -> task-router category mappings.

Only mappings with a defensible semantic relationship are included. Unknown
source categories must be skipped and reported, never guessed into the 24-row
router vocabulary. ``artificial_analysis_intelligence`` is deliberately a
separate category because AA's intelligence index is a broad composite, not a
single task capability.
"""

AA_CATEGORY = "artificial_analysis_intelligence"
AA_CODING_CATEGORY = "artificial_analysis_coding"
AA_AGENTIC_CATEGORY = "artificial_analysis_agentic"

# AA composite indexes are retained in dedicated categories rather than being
# force-mapped to one of the 24 task categories. Values are normalized to 0-1
# at ingest and retain their feed value in source_ref.
AA_INDEX_CATEGORIES = {
    "coding_index": AA_CODING_CATEGORY,
    "agentic_index": AA_AGENTIC_CATEGORY,
    "intelligence_index": AA_CATEGORY,
}

CATEGORY_MAP = {
    ("models", "3d"): "creative",
    ("models", "asciiart"): "creative",
    ("models", "codecategories"): "code_gen",
    ("models", "dataviz"): "creative",
    ("models", "gamedev"): "long_horizon",
    ("models", "graphicdesign"): "creative",
    ("models", "image"): "creative",
    ("models", "imageediting"): "creative",
    ("models", "logo"): "creative",
    ("models", "svg"): "ui_frontend",
    ("models", "uicomponent"): "ui_frontend",
    ("models", "website"): "ui_frontend",
    ("agents", "agenticgamedev"): "agent_tick",
    ("agents", "agentichtmlslides"): "ui_frontend",
    ("agents", "agenticslides"): "spec_docs",
    ("agents", "agenticslides(html)"): "ui_frontend",
    ("agents", "agenticslides(python-pptx)"): "spec_docs",
    ("agents", "androidnative"): "e2e_vision",
    ("agents", "blender3d"): "creative",
    ("agents", "codetovideo"): "e2e_vision",
    ("agents", "fullstack"): "e2e_vision",
    ("agents", "godotgamedev"): "agent_tick",
    ("agents", "htmlslides"): "ui_frontend",
    ("agents", "mobileapps"): "e2e_vision",
    ("agents", "pdf"): "long_doc",
    ("agents", "pptxslides"): "spec_docs",
    ("agents", "python-pptxslides"): "spec_docs",
    ("agents", "spreadsheet"): "mechanical",
    ("agents", "webapps"): "e2e_vision",
}

# Populated by the ingest as an audit trail for categories intentionally skipped.
UNMAPPED_REASONS: dict[tuple[str, str], str] = {}
