#!/usr/bin/env python3
"""Seed the task-router into the real routing registry (additive).
- level_defs: -5..+5 percentile scale
- model_perf: 24 categories (10 benchmark cols + 14 from profile-tag estimates + benchmark overlays)
- category_levels: per-category percentile thresholds (11 levels)
- model_tier: per (provider, model, category) signed level
- task_profiles + requirements: P0_FORE / P5_VISION_E2E / P7_MOCK / P9_REVIEW
  + P1_CODING / P2_AGENTIC / P3_DOCS / P4_SECURITY (TR-003, 2026-08-27)
- views: v_task_eligible, v_task_chain
Exports tables to the routing namespace JSONL. Run: board venv python."""
import duckdb, json, shutil, os, subprocess, datetime

# Text registry (Bane 2026-08-27): the live store is a gitignored JSON file in
# the task-router repo — NOT a binary duckdb. The seed computes against an
# IN-MEMORY duckdb (engine only, nothing persisted as binary) and writes
# registry.json. ROUTING_REGISTRY override lets maintenance/tests point the
# whole loop at a scratch copy — same pattern as router_spawn.py (TR-005).
# Repo-relative defaults: the project is self-contained (clone → seed → use).
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(_HERE)
REGISTRY = os.environ.get('ROUTING_REGISTRY', os.path.join(_REPO, 'registry.json'))
DATA_DIR = os.environ.get('ROUTING_DATA_DIR', os.path.join(_REPO, 'data', 'tables'))
# The DuckBrain namespace is the S3-backed MIRROR — absent on a fresh clone;
# the seed still runs (it just skips the ns export step).
NS = os.environ.get('ROUTING_NS', '/home/kara/duckbrain/namespaces/routing')

# Base tables come from the committed namespace JSONL (array-per-line, column
# order = duckdb DESCRIBE order) — or from an existing registry.json when the
# ns copy is stale. Types are explicit so the derivation SQL behaves exactly
# as it did against the file DB.
BASE_COLUMNS = {
    'providers': [('id', 'VARCHAR'), ('plan', 'VARCHAR'), ('quota_unit', 'VARCHAR'),
                  ('windows', 'VARCHAR'), ('concurrency', 'INTEGER'),
                  ('tos_class', 'VARCHAR'), ('data_class', 'VARCHAR'),
                  ('valid_from', 'DATE'), ('valid_to', 'DATE'), ('archive', 'BOOLEAN')],
    'models': [('provider', 'VARCHAR'), ('model', 'VARCHAR'),
               ('normalized_price', 'DOUBLE'), ('price_evidence', 'VARCHAR'),
               ('public_price', 'DOUBLE'), ('public_in_per_m', 'DOUBLE'),
               ('public_out_per_m', 'DOUBLE'),
               ('data_class', 'VARCHAR'), ('plan_tier', 'INTEGER'),
               ('perf_agent_tick', 'DOUBLE'), ('perf_long_doc', 'DOUBLE'),
               ('perf_debug', 'DOUBLE'), ('perf_schema', 'DOUBLE'),
               ('perf_e2e_vision', 'DOUBLE'), ('perf_review', 'DOUBLE'),
               ('perf_delegation', 'DOUBLE'), ('perf_guard', 'DOUBLE'),
               ('perf_mock', 'DOUBLE'), ('perf_reasoning', 'DOUBLE'),
               ('valid_from', 'DATE'), ('valid_to', 'DATE'), ('archive', 'BOOLEAN'),
               ('token_factor', 'DOUBLE'),
               ('disabled', 'BOOLEAN'), ('disabled_reason', 'VARCHAR')],
    'benchmarks': [('model', 'VARCHAR'), ('category', 'VARCHAR'), ('score', 'DOUBLE'),
                   ('max_score', 'DOUBLE'), ('source', 'VARCHAR'), ('valid_from', 'DATE')],
    'archetypes': [('id', 'VARCHAR'), ('bar', 'DOUBLE'), ('skill_levels', 'VARCHAR'),
                   ('notes', 'VARCHAR')],
    'projects': [('id', 'VARCHAR'), ('sensitivity', 'VARCHAR'), ('board_type', 'VARCHAR'),
                 ('stack', 'VARCHAR'), ('profile', 'VARCHAR')],
    'task_profiles': [('id', 'VARCHAR'), ('title', 'VARCHAR'),
                      ('created_at', 'VARCHAR'),
                      ('max_consecutive_per_provider', 'INTEGER'),
                      ('max_total_per_provider', 'INTEGER')],
    'task_profile_requirements': [('task_id', 'VARCHAR'), ('category', 'VARCHAR'),
                                  ('level', 'INTEGER')],
}


def _load_base_rows(name):
    """Base-table rows from the in-repo data/tables/*.jsonl (keyed records —
    committed, self-contained), falling back to the ns mirror (array format)
    or an existing registry.json."""
    # 1) in-repo committed data (primary; works on a fresh clone)
    path = os.path.join(DATA_DIR, f'{name}.jsonl')
    try:
        rows = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if rows:
            cols = [c[0] for c in BASE_COLUMNS[name]]
            return [tuple(r.get(c) for c in cols) for r in rows]
    except Exception:
        pass
    # 2) existing registry.json (pre-migration convenience)
    try:
        with open(REGISTRY) as f:
            doc = json.load(f)
        rows = (doc.get('tables') or {}).get(name)
        if rows:
            cols = [c[0] for c in BASE_COLUMNS[name]]
            return [tuple(r.get(c) for c in cols) for r in rows]
    except Exception:
        pass
    # 3) ns mirror (array-per-line)
    path = f'{NS}/tables/{name}.jsonl'
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(tuple(json.loads(line)))
    return out


con = duckdb.connect(':memory:')
for t, cols in BASE_COLUMNS.items():
    rows = _load_base_rows(t)
    con.execute(f"CREATE TABLE {t} ({', '.join(f'{n} {ty}' for n, ty in cols)})")
    if rows:
        con.executemany(f"INSERT INTO {t} VALUES ({','.join('?' * len(cols))})", rows)
    print(f'loaded {t:<12} {len(rows):>3} rows')

CATS = ['agent_tick','long_doc','debug','schema','e2e_vision','review','delegation',
        'guard','mock','reasoning','code_gen','refactor','terminal','mechanical','test',
        'math','tool_use','long_horizon','vision','ui_frontend','spec_docs','creative',
        'multilingual','security']
OLD = ['agent_tick','long_doc','debug','schema','e2e_vision','review','delegation','guard','mock','reasoning']

# ---------- 1. level_defs: the -5..+5 scale (percentiles) ---------------------
con.execute("DROP TABLE IF EXISTS level_defs")
con.execute("""CREATE TABLE level_defs (level INTEGER PRIMARY KEY, label VARCHAR, qcol VARCHAR)""")
lv = [(-5,'-----','q01'),(-4,'----','q05'),(-3,'---','q10'),(-2,'--','q20'),(-1,'-','q35'),
      (0,'0','q50'),(1,'+','q65'),(2,'++','q80'),(3,'+++','q90'),(4,'++++','q95'),(5,'+++++','q99')]
con.executemany("INSERT INTO level_defs VALUES (?,?,?)", lv)

# ---------- 2. model_perf: metrics ONCE PER MODEL (Bane 2026-08-27) ----------
# Evidence describes the MODEL (weights), not the provider lane. A model
# served by N providers has ONE perf row per category; lanes inherit the same
# tier. Per-provider lane disabling is an explicit opt-in (models.disabled +
# disabled_reason), never the default.
con.execute("DROP TABLE IF EXISTS model_perf")
con.execute("""
CREATE TABLE model_perf AS
SELECT model, replace(category, 'perf_', '') AS category, max(perf) AS perf
FROM (UNPIVOT (SELECT model, perf_agent_tick, perf_long_doc, perf_debug, perf_schema,
                      perf_e2e_vision, perf_review, perf_delegation, perf_guard, perf_mock, perf_reasoning
               FROM models WHERE valid_to IS NULL AND archive = false
                            AND (disabled IS NULL OR NOT disabled))
      ON perf_agent_tick, perf_long_doc, perf_debug, perf_schema,
         perf_e2e_vision, perf_review, perf_delegation, perf_guard, perf_mock, perf_reasoning
      INTO NAME category VALUE perf)
WHERE perf IS NOT NULL
GROUP BY model, category
""")

# ---------- 3. benchmark overlays for NEW categories --------------------------
# source substring -> new category (rel score reused from benchmarks table)
BENCH_OVERLAY = {
    'AIME26': ['math'],
    'Terminal-Bench': ['terminal'],
    'battery-T1-TOOL': ['tool_use'],
    'battery-T2-CODE': ['code_gen', 'refactor'],
    'battery-T5-DEBUG': ['code_gen'],
    'SWE-rebench': ['code_gen'],
    'ApexBench': ['vision'],
    'BrowseComp multimodal': ['vision'],
    '106 hard browser tasks': ['vision'],
    'ExploitBench': ['security'],
    'BenchLM': ['spec_docs'],
}
overlay = []  # (provider, model, category, rel_score)
for src, cats in BENCH_OVERLAY.items():
    rows = con.execute("SELECT model, category, score, max_score FROM benchmarks WHERE source LIKE ?",
                       [f'%{src}%']).fetchall()
    for model, _, score, mx in rows:
        if not mx:
            continue
        for c in cats:
            overlay.append((model, c, float(score) / float(mx)))
# map benchmark model names (e.g. cline-pass/glm-5.3) to registry (provider, model)
model_ids = {}
for p, m in con.execute("SELECT DISTINCT provider, model FROM models").fetchall():
    model_ids.setdefault(m.lower().replace('-pass', '').replace('-cloud', ''), []).append((p, m))

# ---------- 4. profile-tag estimates for the 14 new categories ----------------
TAG2CAT = {
    'code-generation': ['code_gen'], 'debugging': ['debug'], 'refactoring': ['refactor'],
    'terminal': ['terminal'], 'architecture': ['schema'], 'long-context': ['long_doc'],
    'vision': ['vision'], 'advanced-vision': ['vision'], 'ui-analysis': ['ui_frontend'],
    'frontend': ['ui_frontend'], 'math': ['math'], 'agentic-coding': ['agent_tick', 'long_horizon'],
    'autonomous-work': ['long_horizon'], 'multi-step-reasoning': ['reasoning'],
    'complex-reasoning': ['reasoning'], 'concise-output': ['mechanical'],
    'fast-mechanical': ['mechanical'], 'file-editing': ['code_gen'], 'testing': ['test'],
    'test-execution': ['test'], 'test-writing': ['test'], 'code-review': ['review'],
    'security': ['security'], 'creative-writing': ['creative'], 'brainstorming': ['creative'],
    'tool-use': ['tool_use'], 'tool-calling-stability': ['tool_use'],
    'spec-writing': ['spec_docs'], 'documentation': ['spec_docs'], 'structured-data': ['spec_docs'],
    'browser': ['e2e_vision'], 'screenshots': ['e2e_vision', 'vision'],
    'cli-automation': ['terminal'], 'gui-automation': ['terminal'],
    'planning': ['long_horizon'], 'subagent-coordination': ['long_horizon'],
    'long-horizon-agents': ['long_horizon'], 'long-running': ['long_horizon'],
    'research-orchestration': ['long_horizon'], 'multi-agent': ['long_horizon'],
    'anti-hallucination-grounding': ['guard'], 'filtering': ['mechanical'],
    'scoped-implementation': ['code_gen'], 'creative': ['creative'],
}
TAG2PERF = {'+++': 0.95, '++': 0.85, '+': 0.72, '0': 0.60, '-': 0.45, '--': 0.30, '---': 0.15}

# profile name substrings -> (provider, model) pairs (registry ids)
PROFILE_MODELS = {
    'ox-alpha': [('zai-glm', 'glm-5.3-flash')],  # ox-alpha revealed as GLM-5.3-Flash 2026-08-26; free lane ended
    'glm-5.3-flash': [('zai-glm', 'glm-5.3-flash')],
    'qwen3.8-flash': [('opencode-go', 'qwen3.8-flash')],
    'deepseek v4 pro': [('deepseek', 'deepseek-v4-pro'), ('ollama-cloud', 'deepseek-v4-pro')],
    'deepseek v4 flash': [('deepseek', 'deepseek-v4-flash'), ('ollama-cloud', 'deepseek-v4-flash:0731'), ('clinepass', 'deepseek-v4-flash'),
                          ('opencode-go', 'deepseek-v4-flash')],
    'kimi k3': [('kimi-for-coding', 'k3'), ('ollama-cloud', 'kimi-k3'), ('clinepass', 'kimi-k3'),
                ('synthetic', 'kimi-k3'), ('neuralwatt', 'kimi-k3'), ('opencode-go', 'kimi-k3')],
    'minimax m3': [('minimax', 'minimax-m3'), ('ollama-cloud', 'minimax-m3'), ('clinepass', 'minimax-m3')],
    'glm-5.3': [('clinepass', 'glm-5.3'), ('zai-glm', 'glm-5.3'), ('ollama-cloud', 'glm-5.3')],
    'glm-5.2': [('ollama-cloud', 'glm-5.2'), ('zai-glm', 'glm-5.2'), ('synthetic', 'glm-5.2'),
                ('opencode-go', 'glm-5.2'), ('clinepass', 'glm-5.2')],
    'gpt-5.6 sol': [('openai-codex', 'gpt-5.6-sol')],
    'gpt-5.6 terra': [('openai-codex', 'gpt-5.6-terra')],
    'gpt-5.6 luna': [('openai-codex', 'gpt-5.6-luna')],
    'step 3': [('stepfun', 'step-3.7-flash'), ('stepfun', 'step-3.5-flash')],
    'grok 4.6': [],  # not in registry models yet
    'grok 4.5': [('grok-build', 'grok-4.5'), ('opencode-go', 'grok-4.5')],
    'grok 4.20': [],
    'hy3': [('opencode-go', 'hy3')],
    'longcat': [('opencode-go', 'longcat-2.0')],
    'nemotron': [],
    'kimi k2': [('clinepass', 'kimi-k2.7-code')],
    'mimo': [('opencode-go', 'mimo-v2.5'), ('clinepass', 'mimo-v2.5')],
    'mimo-v2.5-pro': [('opencode-go', 'mimo-v2.5-pro'), ('clinepass', 'mimo-v2.5-pro')],
    'hy4-preview': [('opencode-go', 'hy4-preview')],
    'qwen3.7-max': [('opencode-go', 'qwen3.7-max'), ('clinepass', 'qwen3.7-max')],
    'qwen3.8': [('opencode-go', 'qwen3.8-max')],
    'gpt-oss': [('groq', 'gpt-oss-120b'), ('synthetic', 'gpt-oss-120b')],
    'qwen3.6-27b': [('groq', 'qwen3.6-27b')],
}

# profiles with their tag dicts (name -> {tag: level})
PROFILE_TAGS = {
    'ox-alpha': {'code-generation': '++', 'terminal': '++', 'refactoring': '++', 'long-context': '++',
                 'architecture': '+', 'debugging': '+'},
    'deepseek v4 pro': {'code-generation': '++', 'debugging': '++', 'terminal': '++', 'refactoring': '++',
                        'architecture': '+', 'long-context': '+', 'vision': '-', 'ui-analysis': '-',
                        'frontend': '-', 'debugging-t5': '--',
                        # 2026-08-27 fleet-evidence fills: native tool-calling +
                        # test-capable (deepseek API; used for fleet tests)
                        'tool-use': '++', 'testing': '+'},
    'deepseek v4 flash': {'code-generation': '++', 'terminal': '++', 'test-execution': '++', 'debugging': '++',
                          'concise-output': '+', 'file-editing': '+', 'testing': '+',
                          # 2026-08-27 fleet-chat fix: the fleet workhorse (50k+ ticks,
                          # gap-free reports) was rated below kimi-k2.7-code on
                          # reasoning/long_doc/review via neutral fills — corrected with
                          # fleet-quality evidence (reasoning '++', long-context '+',
                          # code-review '+'); see docs/registry-text-migration note.
                          'complex-reasoning': '++', 'long-context': '+', 'code-review': '+',
                          'tool-use': '++',  # native function-calling (deepseek API)
                          'documentation': '+',  # fleet reports evidence (50k+ ticks)
                          'advanced-vision': '-', 'architecture': '--'},
    'kimi k3': {'agentic-coding': '++', 'autonomous-work': '++', 'vision': '+', 'long-context': '++',
                'code-generation': '+', 'debugging': '+', 'multi-step-reasoning': '+', 'frontend': '+',
                'ui-analysis': '-',
                'tool-use': '++', 'testing': '+', 'refactoring': '+'},  # coding-plan lane, tool-native
    'minimax m3': {'code-generation': '++', 'long-context': '++', 'agentic-coding': '++', 'debugging': '+',
                   'terminal': '+', 'vision': '+', 'ui-analysis': '-', 'complex-architecture': '-',
                   'refactoring': '-',
                   'tool-use': '+', 'testing': '+'},  # API tool-calling + tests (fleet evidence)
    'glm-5.3': {'code-generation': '++', 'code-review': '++', 'terminal': '++', 'agentic-coding': '++',
                'security': '++', 'debugging': '+', 'long-context': '+', 'architecture': '+',
                'creative-writing': '-', 'vision': '-',
                'testing': '+', 'refactoring': '+'},  # fleet coding evidence fills
    'glm-5.3-flash': {'code-generation': '++', 'code-review': '++', 'terminal': '++', 'agentic-coding': '++',
                      'security': '++', 'debugging': '+', 'long-context': '+', 'architecture': '+',
                      # 2026-08-27 fleet-chat fix: missing tool-use rating (glm-5.2 had it)
                      # neutral-filled tool_use to tier 0 and excluded the fleet's best
                      # agent_tick model from every tool_use>=2 chain — parity with glm-5.2.
                      'tool-use': '++',
                      'testing': '+', 'refactoring': '+',  # fleet coding evidence fills
                      'documentation': '+',  # fleet docs evidence
                      'vision': '0', 'creative-writing': '-'},
    'qwen3.8-flash': {'code-generation': '++', 'agentic-coding': '++', 'debugging': '+', 'terminal': '+',
                      'testing': '+', 'vision': '+', 'long-context': '+', 'architecture': '-',
                      'creative-writing': '-',
                      'tool-use': '+', 'refactoring': '+', 'spec-writing': '+'},  # fleet evidence fills
    'glm-5.2': {'code-generation': '++', 'code-review': '++', 'terminal': '++', 'tool-use': '++',
                'debugging': '+', 'long-context': '+', 'architecture': '+', 'frontend': '+',
                'creative-writing': '-', 'vision': '-',
                # 2026-08-27 fleet-evidence fills: security ++ (family parity —
                # glm-5.3/5.3-flash both ++; glm-5.2 was neutral-filled and only
                # cleared security=3 via quantile inflation), testing/refactoring +
                'security': '++', 'testing': '+', 'refactoring': '+',
                'documentation': '+'},  # fleet docs evidence
    'gpt-5.6 sol': {'architecture': '+++', 'terminal': '+++', 'browser': '+++', 'complex-reasoning': '++',
                    'debugging': '++', 'planning': '++', 'subagent-coordination': '++',
                    'long-running': '++', 'code-generation': '+', 'code-review': '+',
                    'fast-mechanical': '-', 'concise-output': '-'},
    'gpt-5.6 terra': {'spec-writing': '+++', 'documentation': '+++', 'structured-data': '++',
                      'testing': '++', 'scoped-implementation': '++', 'code-review': '+',
                      'complex-architecture': '-', 'performance': '-'},
    'gpt-5.6 luna': {'test-execution': '+++', 'vision': '+++', 'browser': '+++', 'screenshots': '+++',
                     'debugging': '++', 'terminal': '++', 'architecture': '-',
                     'complex-reasoning': '-', 'long-form-processing': '-'},
    'step 3': {'agentic-coding': '+++', 'testing': '+++', 'browser': '+++', 'cli-automation': '+++',
               'code-generation': '+', 'vision': '+', 'screenshots': '+', 'gui-automation': '+',
               'architecture': '-', 'complex-reasoning': '-', 'long-context': '-',
               'tool-use': '+', 'refactoring': '+'},  # fleet evidence fills
    'hy3': {'frontend': '++', 'ui-work': '++', 'html-css': '++', 'file-editing': '+',
            'concise-output': '+', 'tool-calling-stability': '+', 'anti-hallucination-grounding': '+',
            'architecture': '-', 'complex-reasoning': '-'},
    'longcat': {'long-context': '++', 'brainstorming': '++', 'creative': '++', 'agentic-coding': '+',
                'code-generation': '++', 'terminal': '+'},  # 2026-08-31: SWE-Pro 59.5/TB 70.8 (vendor)
    'kimi k2': {'agentic-coding': '++', 'code-generation': '+', 'long-context': '+',
                'tool-use': '++', 'testing': '+', 'refactoring': '+'},  # k2.7-code lane, tool-native
    'mimo': {'code-generation': '+', 'terminal': '+', 'debugging': '+', 'concise-output': '+',
             'tool-use': '++', 'testing': '+', 'refactoring': '+'},  # opencode-go agentic workhorse
    # 2026-08-31 research (research-bench-2026-08-31): mimo-v2.5-pro (Xiaomi) —
    # TB 2.0 68.4 (vs MiniMax M2.7 57), coding avg 57.2, frontier coding at
    # 40-60% fewer tokens (r/LLMDevs).
    'mimo-v2.5-pro': {'code-generation': '++', 'terminal': '++', 'debugging': '+',
                      'concise-output': '+', 'tool-use': '++', 'testing': '+', 'refactoring': '+'},
    # 2026-08-31 research: hy4-preview = Tencent Hy4 770B MoE — SWE-bench
    # Multilingual 82.9 (vs GLM-5.3 81.3 / K3 80.8), TB 2.1 85.4, DeepSWE 64.3,
    # GPQA 92.3. Top-tier open model, preview stage.
    'hy4-preview': {'code-generation': '++', 'terminal': '++', 'agentic-coding': '++',
                    'debugging': '+', 'long-context': '+', 'architecture': '+',
                    'complex-reasoning': '+', 'testing': '+', 'refactoring': '+'},
    # 2026-08-31 research: qwen3.7-max — SWE-bench Pro 60.6 (launch-best), SWE-V
    # 80.4, TB 2.0 69.7, GPQA 92.4, 1M ctx native extended thinking.
    'qwen3.7-max': {'code-generation': '++', 'terminal': '++', 'agentic-coding': '++',
                    'debugging': '+', 'long-context': '++', 'architecture': '+',
                    'complex-reasoning': '++', 'testing': '+', 'refactoring': '+'},
    'qwen3.8': {'code-generation': '++', 'debugging': '+', 'agentic-coding': '+'},
    'gpt-oss': {'reasoning': '+', 'tool-use': '++', 'code-generation': '+',
                'testing': '+', 'refactoring': '+'},  # open-weight agentic; native tool-calling
    'qwen3.6-27b': {'concise-output': '+', 'filtering': '+', 'mock-data': '+'},
    # 2026-08-28: grok-4.5 profile (was [] — no perf rows -> never routable).
    # Tags from xAI system card + fleet battery: Terminal-Bench 2.1 83.3 (++),
    # SWE-bench Pro 64.7 / DeepSWE 1.0 62.0 (+), battery T1-TOOL 3/4 tool-use (+);
    # long_horizon left BLANK on purpose (DeepSWE 1.1 53, SWE Marathon discipline
    # complaints) — benchmark rows document it, BLANK default keeps it honest.
    'grok 4.5': {'terminal': '++', 'code-generation': '+', 'refactoring': '+',
                 'tool-use': '+'},
}

# ---------- 4b. TR-002: quality estimates for degenerate categories ----------
# guard/mock were saturated by battery-T4-INSTR-floor (pass/fail floor test:
# 40+ models at 1.0, bimodal 0.0/1.0) and multilingual was neutral-filled
# 0.50 (all models equal -> every model tier 5, scale a no-op).
#
# Data policy (documented in docs/category-data-quality.md, 2026-08-27):
#  - Surveyed mid-range values (0.6-0.9, entered from real vendor signals)
#    are PRESERVED verbatim - only degenerate values are replaced.
#  - Replaced values are family-prior estimates: aliases/variants of surveyed
#    models inherit the sibling value (glm-5.3-offpeak == glm-5.3, gpt-oss:20b
#    == gpt-oss-20b, gemma-4:31b == gemma-4-31b, k3 == Kimi K3, ...).
#  - guard = agentic reliability / anti-hallucination grounding (TAG2CAT
#    'anti-hallucination-grounding'), calibrated to surveyed anchors
#    (glm-5.3 0.80, gpt-oss-120b 0.80, qwen3.8 0.72-0.75, qwen3.6-27b 0.70).
#  - mock = data-fabrication realism, calibrated to surveyed anchors
#    (qwen3.8-flash 0.90, gpt-oss-120b/qwen3.6-27b 0.85, glm-5.3 family 0.60).
#  - multilingual = family training-data priors (Chinese-first families
#    strong EN+ZH, Western flagships strong EN+EU, edge models weaker).
# Values are model-NAME keyed and apply to every provider row of that model.
# NOTE (Bane 2026-08-27): the estimate TABLE itself lives in
# data/tables/quality_estimates.jsonl — this code block is documentation only;
# the dict is loaded by _load_quality_estimates() below.

def _load_quality_estimates():
    """Load quality estimates from the DATA file (Bane 2026-08-27: provider/
    model facts live in data, never hardcoded in scripts). Rows are
    model-keyed: {model, guard?, mock?, multilingual?, note?}. Missing file
    or row = no estimate (visible gap), never a code fallback."""
    path = os.path.join(DATA_DIR, 'quality_estimates.jsonl')
    out = {}
    if not os.path.exists(path):
        print(f'WARNING: {path} missing — quality estimates skipped (research agent to fill)')
        return out
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        out[r['model']] = (r.get('guard'), r.get('mock'), r.get('multilingual'))
    return out


QUALITY_ESTIMATES = _load_quality_estimates()

def apply_quality_estimates():
    """TR-002: replace degenerate guard/mock/multilingual perfs with documented
    estimates. Only values in {0.0, 1.0} (guard/mock) or 0.50 (multilingual) are
    replaced - surveyed mid values are preserved. Returns rows updated."""
    n = 0
    for name, (g, m, ml) in QUALITY_ESTIMATES.items():
        for cat, v in (('guard', g), ('mock', m), ('multilingual', ml)):
            if v is None:
                continue
            cur = con.execute(
                "SELECT perf FROM model_perf WHERE model=? AND category=?",
                [name, cat]).fetchone()
            if not cur:
                continue
            degenerate = (cat in ('guard', 'mock') and cur[0] in (0.0, 1.0)) or \
                         (cat == 'multilingual' and abs(cur[0] - 0.50) < 0.001)
            if degenerate:
                con.execute(
                    "UPDATE model_perf SET perf=? WHERE model=? AND category=?",
                    [v, name, cat])
                n += 1
    return n

def seed_estimates():
    """Insert profile-tag estimates for NEW categories (skip cats already in model_perf).
    Evidence is per MODEL — one row per (model, category), never per lane."""
    new_cats = set(CATS) - set(OLD)
    # live models only — a model with no live lane (e.g. archived rows like
    # opencode-go/ox-alpha-free) must not leak into model_perf (TR-008)
    live_models = {r[0] for r in con.execute(
        "SELECT DISTINCT model FROM models WHERE valid_to IS NULL AND archive = false").fetchall()}
    n = 0
    for pname, pairs in PROFILE_MODELS.items():
        tags = PROFILE_TAGS.get(pname, {})
        taglevels = {}
        for tag, lvl in tags.items():
            for c in TAG2CAT.get(tag, []):
                taglevels[c] = max(taglevels.get(c, 0), TAG2PERF.get(lvl, 0.60))
        for prov, model in pairs:
            if model not in live_models:
                continue  # profile references a model the registry doesn't serve
            for c in new_cats:
                v = taglevels.get(c)
                if v is None:
                    continue
                if con.execute("SELECT 1 FROM model_perf WHERE model=? AND category=?",
                               [model, c]).fetchone():
                    continue
                con.execute("INSERT INTO model_perf VALUES (?,?,?)", [model, c, v])
                n += 1
    # BLANK default (Bane 2026-08-27): a model with nothing set for a category
    # stays BLANK — no fabricated plus/minus, no 0.50 neutral fill. The resolver
    # treats a missing tier as -1 (slightly below median): clears lenient bars,
    # fails 0 and up. Gaps are surfaced by router_gaps.py (perf/tiers dims).
    return n

def apply_overlay():
    """Benchmark overlay: set new-category perfs from benchmark rel scores (only where estimate is neutral 0.50)."""
    n = 0
    for model, cat, rel in overlay:
        # benchmark names may differ in case from registry names (MiniMax-M3
        # vs minimax-m3) — match case-insensitively
        cur = con.execute("SELECT perf FROM model_perf WHERE lower(model)=? AND category=?",
                          [model.lower(), cat]).fetchone()
        if cur and abs(cur[0] - 0.50) < 0.001:
            con.execute("UPDATE model_perf SET perf=? WHERE lower(model)=? AND category=?",
                        [rel, model.lower(), cat])
            n += 1
    return n

seed_estimates()
apply_overlay()
n_est = apply_quality_estimates()
print('quality estimate rows updated (TR-002):', n_est)

# dedupe: benchmark overlays may have inserted the same (model, category) twice
con.execute("DROP TABLE IF EXISTS model_perf_dedup")
con.execute("""
CREATE TABLE model_perf_dedup AS
SELECT model, category, max(perf) AS perf FROM model_perf GROUP BY 1,2""")
con.execute("DROP TABLE model_perf")
con.execute("ALTER TABLE model_perf_dedup RENAME TO model_perf")

# ---------- 5. category_levels + model_tier -----------------------------------
con.execute("DROP TABLE IF EXISTS cat_q")
# quantiles over UNIQUE models (dedupe lanes): the same weights served by N
# providers must not count N times in the scale (2026-08-27: clinepass added
# 6 lanes of deepseek-v4-flash -> duplicated evidence shifted tool_use q90 and
# silently dropped the fleet's workhorses from tier 5 to 4).
con.execute("""
CREATE TABLE cat_q AS
SELECT category,
       quantile_cont(perf, 0.01) AS q01, quantile_cont(perf, 0.05) AS q05,
       quantile_cont(perf, 0.10) AS q10, quantile_cont(perf, 0.20) AS q20,
       quantile_cont(perf, 0.35) AS q35, quantile_cont(perf, 0.50) AS q50,
       quantile_cont(perf, 0.65) AS q65, quantile_cont(perf, 0.80) AS q80,
       quantile_cont(perf, 0.90) AS q90, quantile_cont(perf, 0.95) AS q95,
       quantile_cont(perf, 0.99) AS q99
FROM (SELECT category, model, max(perf) AS perf
      FROM model_perf GROUP BY category, model)
GROUP BY category""")
con.execute("DROP TABLE IF EXISTS category_levels")
con.execute("""
CREATE TABLE category_levels AS
SELECT q.category, l.level, l.label, q.min_perf
FROM level_defs l
JOIN (UNPIVOT cat_q ON q01, q05, q10, q20, q35, q50, q65, q80, q90, q95, q99
      INTO NAME qcol VALUE min_perf) q ON q.qcol = l.qcol""")
con.execute("DROP TABLE IF EXISTS model_tier")
con.execute("""
CREATE TABLE model_tier AS
SELECT mp.model, mp.category, mp.perf, max(cl.level) AS tier
FROM model_perf mp JOIN category_levels cl
  ON cl.category = mp.category AND cl.min_perf <= mp.perf
GROUP BY mp.model, mp.category, mp.perf""")
# BLANK default (Bane 2026-08-27): models without a perf row in a category have
# NO tier row — the resolver treats a missing tier as -1, never 0 and never
# an inflated neutral.

# ---------- 6. task profiles ---------------------------------------------------
con.execute("DROP TABLE IF EXISTS task_profiles")
con.execute("DROP TABLE IF EXISTS task_profile_requirements")
con.execute("CREATE TABLE task_profiles (id VARCHAR PRIMARY KEY, title VARCHAR, created_at TIMESTAMP, "
            "max_consecutive_per_provider INTEGER, max_total_per_provider INTEGER)")
con.execute("CREATE TABLE task_profile_requirements (task_id VARCHAR, category VARCHAR, level INTEGER, PRIMARY KEY (task_id, category))")

# Profile levels re-based to the FIXED percentile scale (TR-002, 2026-08-27):
# the model_tier join previously leaked levels across categories (no
# cl.category condition), inflating every tier and making the seeded levels
# (written against the inflated scale) far stricter than intended. Each
# profile below uses the tightest honest level that keeps its pre-TR-002
# chain membership -> heads, ordering and scheduler behavior unchanged
# (AC3). See docs/category-data-quality.md.
#
# DATA-DRIVEN SOURCE (Bane 2026-08-27): the live profiles come from the
# committed data rows in data/tables/task_profiles.jsonl +
# task_profile_requirements.jsonl — loaded exactly like every other table
# below. Adding a profile = append data rows + re-run the seed (NO code
# edit). This dict is ONLY a bootstrap fallback for a fresh clone whose data
# files are empty; it must mirror the data rows for new profiles.
PROFILES = {
    'P0_FORE': ("Default foreman: board ops, audit, dispatch, gap-free reports",
                # 2026-08-27 Bane design: capabilities drive the chain — pass
                # what the foreman task needs, router returns (model, provider)
                # pairs that clear ALL bars, ordered by normalized price.
                # Levels = min tier across the intended workhorse set
                # {glm-5.3-flash, glm-5.2, deepseek-v4-flash:0731, kimi-k2.7-code,
                # kimi-k3} per category (TR-002 recipe, honest -1-blank scale):
                # excludes mimo-v2.5 (terminal -2), minimax-m3 / deepseek-v4-pro
                # (agent_tick -1), gpt-oss (below bars); keeps the fleet's
                # agentic workhorses in chain.
                # BANE 2026-08-27 (2nd correction): tool_use REMOVED from P0_FORE.
                # Evidence: tool_use ratings are a cliff artifact, not knowledge —
                # category_levels ladder: perf 0.72-0.83 -> tier -2, perf >=0.85 ->
                # tier 5; NOTHING produces tiers 0-4; every hand-planted 5 has
                # perf EXACTLY 0.85; only 1 real benchmark row exists (perf=None).
                # Tool calling is table stakes in 2026; foreman = board ops/git/
                # dispatch, not expert agentic coding. Keep tool_use in P1/P2.
                # Admits gpt-5.6-luna + gpt-5.6-sol (failed ONLY on tool_use).
                # NOTE: seed agent — keep tool_use OUT of P0_FORE on re-seed;
                # fix the category_levels cliff for tool_use (real ladder).
                # BANE 2026-08-27 (3rd): profile = MINIMUMS, not dreams. Every bar
                # is a floor: agent_tick/delegation at ++ (mid) = instruction
                # following + worker dispatch at solid level; reasoning/long_doc
                # at 0 = average; code_gen/debug/terminal/review lenient; -3
                # floors only exclude catastrophic. When REAL tool-call ratings
                # exist, re-add tool_use=0 (AVERAGE) as the standing floor — never
                # world-class. Do not re-inflate bars to favorites' hand-ratings.
                {'agent_tick': 2, 'delegation': 2,
                 'reasoning': 0, 'long_doc': 0, 'debug': -2,
                 'terminal': -1, 'review': -1, 'schema': 1, 'code_gen': -1,
                 'creative': -3, 'vision': -3, 'e2e_vision': -2,
                 'mock': -3, 'guard': -3}),
    'P5_VISION_E2E': ("Frontend E2E / visual QA",
                      {'e2e_vision': 1, 'vision': 1, 'terminal': 0, 'debug': -2,
                       'reasoning': -1, 'long_doc': 0, 'creative': -3}),
    'P7_MOCK': ("Mock data / test-loop driving",
                {'mock': -3, 'mechanical': 2, 'code_gen': -2, 'reasoning': -1,
                 'long_doc': -2, 'creative': -3}),
    'P9_REVIEW': ("Code review / security-critical diffs",
                  {'review': -2, 'security': -1, 'code_gen': -2, 'reasoning': -1,
                   'schema': -1, 'mock': -3, 'creative': -3, 'e2e_vision': -2}),
    # TR-013 re-base (2026-08-27): BLANK default is -1 (Bane) — a model with no
    # data in a category gets NO tier, resolved as -1. The previous levels were
    # calibrated against the neutral-fill (0.50 -> tier 0) scale where blanks
    # cleared everything >= 0; on the honest scale they produced EMPTY chains
    # (P1/P3/P9: no model cleared refactor=3 AND test=2 etc.). Levels below are
    # the tightest honest values keeping the fleet's intended workhorses in
    # chain (TR-002 precedent: min tier across the intended set per category).
    # They tighten automatically as the research agent fills benchmark/
    # sentiment evidence (router_gaps.py + model-registry-data-quality cron).
    'P1_CODING': ("Fleet coding: feature work, refactors, tests, bug fixes",
                  {'code_gen': -2, 'refactor': -5, 'test': 0, 'debug': -3}),
    'P2_AGENTIC': ("Agentic autonomy: ticks, tool use, delegation, long-horizon runs",
                   {'agent_tick': 0, 'tool_use': 0, 'delegation': 0, 'long_horizon': 1}),
    'P3_DOCS': ("Specs + long-form docs + review",
                {'long_doc': -1, 'spec_docs': 0, 'review': -1}),
    'P4_SECURITY': ("Security-critical: audits, guardrails, secure review",
                    {'security': 2, 'review': 0, 'guard': 0}),
    # P6_DEFAULT (Bane 2026-08-27): the ONE default chain for the bulk of cron
    # work — syncs, monitors, reports, infra, research feeds. Most crons
    # (71/147 duckbrain-sync + monitors + infra) share this chain; specialized
    # crons (foreman/coding/agentic/security/vision/docs) get their own.
    # Levels = honest minimums (Bane 3rd correction): agent_tick/delegation at
    # 0 (median floor — instruction following + worker dispatch), everything
    # else lenient so cheap sub lanes stay eligible. Verified: 39 eligible
    # active priced lanes (36 sub lanes), head $0.033/M, deepseek-v4-flash
    # eligible (always-run guarantee intact). mechanical NOT required (sparse
    # category — de-facto model filter, audit 2026-08-27).
    'P6_DEFAULT': ("Default cron chain: syncs, monitors, reports, infra, research feeds",
                   {'agent_tick': 0, 'delegation': 0, 'code_gen': -2,
                    'reasoning': -1, 'long_doc': -1, 'schema': -1,
                    'terminal': -1, 'tool_use': -1}),
    # P8_SYNC (Bane 2026-08-27): the sync lane — DuckBrain read/write summaries
    # ONLY. Cheaper than P6_DEFAULT: syncs never delegate (delegation -1),
    # never write code, and only need curl-grade terminal (terminal -3 admits
    # mimo-v2.5@opencode-go $0.013 — tool_use 5, agent_tick 2 — 2.5x cheaper
    # than the P6 head deepseek-v4-flash $0.033). agent_tick 0 + schema -1
    # stay as safety floors: the sync still runs an agent loop and must write
    # DuckBrain-valid domains.
    'P8_SYNC': ("Sync lane: DuckBrain read/write summaries only — no delegation, no code, curl-grade terminal",
                {'agent_tick': 0, 'delegation': -1, 'code_gen': -2,
                 'reasoning': -1, 'long_doc': -1, 'schema': -1,
                 'terminal': -3, 'tool_use': -1}),
}
# Preserve existing profile created_at across re-seeds (now() on every run
# made registry.json + ns exports non-idempotent — Bane 2026-08-27 fix).
_existing_ts = {}
try:
    with open(REGISTRY) as f:
        _d = json.load(f)
    for _r in (_d.get('tables') or {}).get('task_profiles') or []:
        _existing_ts[_r.get('id')] = _r.get('created_at')
except Exception:
    pass
# DATA-DRIVEN (Bane 2026-08-27): load profiles from the committed data rows
# first; the PROFILES dict is only a fresh-clone bootstrap.
_prof_rows = _load_base_rows('task_profiles')
_req_rows = _load_base_rows('task_profile_requirements')
if _prof_rows and _req_rows:
    for pid, title, ts, mcp, mtp in _prof_rows:
        con.execute("INSERT INTO task_profiles (id, title, created_at, "
                    "max_consecutive_per_provider, max_total_per_provider) "
                    "VALUES (?, ?, CAST(? AS TIMESTAMP), ?, ?)",
                    [pid, title, ts, mcp, mtp])
    for tid, cat, lvl in _req_rows:
        con.execute("INSERT INTO task_profile_requirements VALUES (?,?,?)",
                    [tid, cat, lvl])
    _src = f'data rows ({len(_prof_rows)} profiles, {len(_req_rows)} reqs)'
else:
    for pid, (title, reqs) in PROFILES.items():
        # explicit column list: the two TR-007 diversity columns stay NULL for the
        # seeded profiles (no overrides → global defaults apply; existing behavior)
        ts = _existing_ts.get(pid) or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        con.execute("INSERT INTO task_profiles (id, title, created_at) VALUES (?, ?, CAST(? AS TIMESTAMP))",
                    [pid, title, ts])
        for c, lvl in reqs.items():
            con.execute("INSERT INTO task_profile_requirements VALUES (?,?,?)", [pid, c, lvl])
    _src = 'PROFILES dict (bootstrap fallback)'
print(f'seeded task profiles from {_src}')

# ---------- 7. views -----------------------------------------------------------
con.execute("DROP VIEW IF EXISTS v_task_eligible")
con.execute("DROP VIEW IF EXISTS v_task_chain")
con.execute("""
CREATE VIEW v_task_eligible AS
SELECT r.task_id, m.provider, m.model, m.normalized_price, m.token_factor, m.plan_tier, m.data_class
FROM models m
JOIN task_profiles tp ON true
JOIN (SELECT DISTINCT task_id FROM task_profile_requirements) r ON r.task_id = tp.id
WHERE m.valid_to IS NULL AND m.archive = false
  AND (m.disabled IS NULL OR NOT m.disabled)
  AND NOT EXISTS (
        SELECT 1 FROM task_profile_requirements rr
        WHERE rr.task_id = r.task_id
          AND NOT EXISTS (SELECT 1 FROM model_tier t
                          WHERE t.model = m.model
                            AND t.category = rr.category AND t.tier >= rr.level))""")
con.execute("""
CREATE VIEW v_task_chain AS
SELECT task_id, provider, model, normalized_price, perf_sum, data_class,
       row_number() OVER (PARTITION BY task_id ORDER BY plan_tier ASC,
                          (normalized_price * token_factor) ASC,
                          model ASC, provider ASC) AS hop
FROM (
  SELECT e.task_id, e.provider, e.model, e.normalized_price, e.token_factor, e.plan_tier, e.data_class,
         (SELECT sum(t.tier) FROM model_tier t
          WHERE t.model = e.model) AS perf_sum
  FROM v_task_eligible e
) WHERE normalized_price IS NOT NULL""")

print('model_perf rows:', con.execute('SELECT count(*) FROM model_perf').fetchone()[0])
print('category_levels rows:', con.execute('SELECT count(*) FROM category_levels').fetchone()[0])
print('model_tier rows:', con.execute('SELECT count(*) FROM model_tier').fetchone()[0])
print('profiles:', con.execute('SELECT id FROM task_profiles ORDER BY 1').fetchall())
print('categories:', con.execute('SELECT count(DISTINCT category) FROM model_perf').fetchone()[0])
for r in con.execute("""
    SELECT category, count(*) FROM model_perf GROUP BY 1 ORDER BY 1""").fetchall():
    print(' ', r)

# ---------- 8. write the text registry + export to namespace ------------------
ALL_TABLES = ['providers', 'models', 'archetypes', 'benchmarks', 'projects',
              'level_defs', 'category_levels', 'model_perf', 'model_tier',
              'task_profiles', 'task_profile_requirements']
def _dump_registry():
    doc = {'version': 3,
           'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
           'source': 'router_seed.py (in-memory duckdb engine)',
           'tables': {}}
    for t in ALL_TABLES:
        cols = [c[0] for c in con.execute(f'DESCRIBE {t}').fetchall()]
        # Full-column ORDER BY — deterministic row order (matches the ns
        # export convention) so registry.json is byte-stable across runs.
        order = ', '.join(str(i + 1) for i in range(len(cols)))
        rows = []
        for r in con.execute(f'SELECT * FROM {t} ORDER BY {order}').fetchall():
            rec = {}
            for c, v in zip(cols, r):
                if isinstance(v, (datetime.datetime, datetime.date)):
                    v = v.isoformat()
                elif v is not None and not isinstance(v, (int, float, bool)):
                    v = str(v)
                rec[c] = v
            rows.append(rec)
        doc['tables'][t] = rows
    # text-only sidecars copied straight from data/tables (no duckdb table):
    # fallback_lanes is a data-file table (Bane 2026-08-27 — provider facts
    # live in data, not code); it must ride along in registry.json so spawn's
    # stdlib fallback path can resolve always-run lanes.
    for t in ('fallback_lanes',):
        src = os.path.join(DATA_DIR, f'{t}.jsonl')
        rows = []
        if os.path.exists(src):
            for line in open(src):
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        doc['tables'][t] = rows
    with open(REGISTRY, 'w') as f:
        json.dump(doc, f, indent=1)
    print('wrote', REGISTRY, f'({os.path.getsize(REGISTRY) / 1024:.1f} KB)')

# ns mirror export (arrays) — only when the DuckBrain namespace exists; a
# fresh clone has no ns and that's fine (data/tables/ is the committed source).
if os.path.isdir(NS):
    os.makedirs(f'{NS}/tables', exist_ok=True)
    for t in ['level_defs', 'model_perf', 'category_levels', 'model_tier',
              'task_profiles', 'task_profile_requirements']:
        cols = [c[0] for c in con.execute(f'DESCRIBE {t}').fetchall()]
        order = ', '.join(str(i + 1) for i in range(len(cols)))
        with open(f'{NS}/tables/{t}.jsonl', 'w') as f:
            for row in con.execute(f'SELECT * FROM {t} ORDER BY {order}').fetchall():
                f.write(json.dumps(row, default=str) + '\n')
    print('exported tables to', NS)
else:
    print('ns mirror absent — skipped ns export (fresh clone is fine)')
_dump_registry()

# Sync the COMMITTED in-repo data/tables/*.jsonl (keyed records) from the
# freshly built registry — the self-contained source of truth.
os.makedirs(DATA_DIR, exist_ok=True)
with open(REGISTRY) as f:
    _doc = json.load(f)
for _t, _rows in _doc['tables'].items():
    with open(f'{DATA_DIR}/{_t}.jsonl', 'w') as f:
        for _r in _rows:
            f.write(json.dumps(_r, ensure_ascii=False) + '\n')
print('synced data/tables ->', DATA_DIR)
