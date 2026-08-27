#!/usr/bin/env python3
"""Seed the task-router into the real routing registry (additive).
- level_defs: -5..+5 percentile scale
- model_perf: 24 categories (10 benchmark cols + 14 from profile-tag estimates + benchmark overlays)
- category_levels: per-category percentile thresholds (11 levels)
- model_tier: per (provider, model, category) signed level
- task_profiles + requirements: P0_FORE / P5_VISION_E2E / P7_MOCK / P9_REVIEW
- views: v_task_eligible, v_task_chain
Exports tables to the routing namespace JSONL. Run: board venv python."""
import duckdb, json, shutil, os, subprocess, datetime

DB = '/home/kara/reports-repo/routing.duckdb'
NS = '/home/kara/duckbrain/namespaces/routing'
con = duckdb.connect(DB)

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

# ---------- 2. model_perf: existing 10 cats from perf_* columns ---------------
con.execute("DROP TABLE IF EXISTS model_perf")
con.execute("""
CREATE TABLE model_perf AS
SELECT provider, model, replace(category,'perf_','') AS category, perf
FROM (UNPIVOT (SELECT provider, model, perf_agent_tick, perf_long_doc, perf_debug, perf_schema,
                      perf_e2e_vision, perf_review, perf_delegation, perf_guard, perf_mock, perf_reasoning
               FROM models WHERE valid_to IS NULL AND archive = false)
      ON perf_agent_tick, perf_long_doc, perf_debug, perf_schema,
         perf_e2e_vision, perf_review, perf_delegation, perf_guard, perf_mock, perf_reasoning
      INTO NAME category VALUE perf)
WHERE perf IS NOT NULL
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
    'deepseek v4 flash': [('deepseek', 'deepseek-v4-flash'), ('ollama-cloud', 'deepseek-v4-flash'),
                          ('ollama-cloud', 'deepseek-v4-flash:0731'), ('clinepass', 'deepseek-v4-flash'),
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
    'grok 4.5': [],
    'grok 4.20': [],
    'hy3': [('opencode-go', 'hy3')],
    'longcat': [('opencode-go', 'longcat-2.0')],
    'nemotron': [],
    'kimi k2': [('clinepass', 'kimi-k2.7-code')],
    'mimo': [('opencode-go', 'mimo-v2.5'), ('clinepass', 'mimo-v2.5')],
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
                        'frontend': '-', 'debugging-t5': '--'},
    'deepseek v4 flash': {'code-generation': '++', 'terminal': '++', 'test-execution': '++', 'debugging': '++',
                          'concise-output': '+', 'file-editing': '+', 'testing': '+',
                          'advanced-vision': '-', 'complex-reasoning': '-', 'architecture': '--'},
    'kimi k3': {'agentic-coding': '++', 'autonomous-work': '++', 'vision': '+', 'long-context': '++',
                'code-generation': '+', 'debugging': '+', 'multi-step-reasoning': '+', 'frontend': '+',
                'ui-analysis': '-'},
    'minimax m3': {'code-generation': '++', 'long-context': '++', 'agentic-coding': '++', 'debugging': '+',
                   'terminal': '+', 'vision': '+', 'ui-analysis': '-', 'complex-architecture': '-',
                   'refactoring': '-'},
    'glm-5.3': {'code-generation': '++', 'code-review': '++', 'terminal': '++', 'agentic-coding': '++',
                'security': '++', 'debugging': '+', 'long-context': '+', 'architecture': '+',
                'creative-writing': '-', 'vision': '-'},
    'glm-5.3-flash': {'code-generation': '++', 'code-review': '++', 'terminal': '++', 'agentic-coding': '++',
                      'security': '++', 'debugging': '+', 'long-context': '+', 'architecture': '+',
                      'vision': '0', 'creative-writing': '-'},
    'qwen3.8-flash': {'code-generation': '++', 'agentic-coding': '++', 'debugging': '+', 'terminal': '+',
                      'testing': '+', 'vision': '+', 'long-context': '+', 'architecture': '-',
                      'creative-writing': '-'},
    'glm-5.2': {'code-generation': '++', 'code-review': '++', 'terminal': '++', 'tool-use': '++',
                'debugging': '+', 'long-context': '+', 'architecture': '+', 'frontend': '+',
                'creative-writing': '-', 'vision': '-'},
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
               'architecture': '-', 'complex-reasoning': '-', 'long-context': '-'},
    'hy3': {'frontend': '++', 'ui-work': '++', 'html-css': '++', 'file-editing': '+',
            'concise-output': '+', 'tool-calling-stability': '+', 'anti-hallucination-grounding': '+',
            'architecture': '-', 'complex-reasoning': '-'},
    'longcat': {'long-context': '++', 'brainstorming': '++', 'creative': '++', 'agentic-coding': '+',
                'code-generation': '+'},
    'kimi k2': {'agentic-coding': '++', 'code-generation': '+', 'long-context': '+'},
    'mimo': {'code-generation': '+', 'terminal': '+', 'debugging': '+', 'concise-output': '+'},
    'qwen3.8': {'code-generation': '++', 'debugging': '+', 'agentic-coding': '+'},
    'gpt-oss': {'reasoning': '+', 'tool-use': '+', 'code-generation': '+'},
    'qwen3.6-27b': {'concise-output': '+', 'filtering': '+', 'mock-data': '+'},
}

def seed_estimates():
    """Insert profile-tag estimates for NEW categories (skip cats already in model_perf)."""
    new_cats = set(CATS) - set(OLD)
    # live models only — archived rows (e.g. opencode-go/ox-alpha-free) must not
    # leak into model_perf via the neutral fill (TR-008)
    valid_pairs = set(con.execute(
        "SELECT provider, model FROM models WHERE valid_to IS NULL AND archive = false").fetchall())
    n = 0
    for pname, pairs in PROFILE_MODELS.items():
        tags = PROFILE_TAGS.get(pname, {})
        taglevels = {}
        for tag, lvl in tags.items():
            for c in TAG2CAT.get(tag, []):
                taglevels[c] = max(taglevels.get(c, 0), TAG2PERF.get(lvl, 0.60))
        for prov, model in pairs:
            if (prov, model) not in valid_pairs:
                continue  # profile references a model the registry doesn't serve
            for c in new_cats:
                v = taglevels.get(c)
                if v is None:
                    continue
                if con.execute("SELECT 1 FROM model_perf WHERE provider=? AND model=? AND category=?",
                               [prov, model, c]).fetchone():
                    continue
                con.execute("INSERT INTO model_perf VALUES (?,?,?,?)", [prov, model, c, v])
                n += 1
    # neutral fill for any (provider, model) missing new-category rows
    for prov, model in valid_pairs:
        have = {r[0] for r in con.execute("SELECT category FROM model_perf WHERE provider=? AND model=?", [prov, model]).fetchall()}
        for c in new_cats:
            if c not in have:
                con.execute("INSERT INTO model_perf VALUES (?,?,?,?)", [prov, model, c, 0.50])
                n += 1
    return n

def apply_overlay():
    """Benchmark overlay: set new-category perfs from benchmark rel scores (only where estimate is neutral 0.50)."""
    n = 0
    for model, cat, rel in overlay:
        pairs = model_ids.get(model.lower().replace('cline-pass', 'clinepass').replace('ollama-cloud', 'ollama').replace('openai-codex', 'openai'), [])
        if not pairs:
            continue
        for prov, m in pairs:
            cur = con.execute("SELECT perf FROM model_perf WHERE provider=? AND model=? AND category=?",
                              [prov, m, cat]).fetchone()
            if cur and abs(cur[0] - 0.50) < 0.001:
                con.execute("UPDATE model_perf SET perf=? WHERE provider=? AND model=? AND category=?",
                            [rel, prov, m, cat])
                n += 1
    return n

seed_estimates()
apply_overlay()

# dedupe: benchmark overlays may have inserted the same (provider, model, category) twice
con.execute("DROP TABLE IF EXISTS model_perf_dedup")
con.execute("""
CREATE TABLE model_perf_dedup AS
SELECT provider, model, category, max(perf) AS perf FROM model_perf GROUP BY 1,2,3""")
con.execute("DROP TABLE model_perf")
con.execute("ALTER TABLE model_perf_dedup RENAME TO model_perf")

# ---------- 5. category_levels + model_tier -----------------------------------
con.execute("DROP TABLE IF EXISTS cat_q")
con.execute("""
CREATE TABLE cat_q AS
SELECT category,
       quantile_cont(perf, 0.01) AS q01, quantile_cont(perf, 0.05) AS q05,
       quantile_cont(perf, 0.10) AS q10, quantile_cont(perf, 0.20) AS q20,
       quantile_cont(perf, 0.35) AS q35, quantile_cont(perf, 0.50) AS q50,
       quantile_cont(perf, 0.65) AS q65, quantile_cont(perf, 0.80) AS q80,
       quantile_cont(perf, 0.90) AS q90, quantile_cont(perf, 0.95) AS q95,
       quantile_cont(perf, 0.99) AS q99
FROM model_perf GROUP BY category""")
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
SELECT mp.provider, mp.model, mp.category, mp.perf, max(cl.level) AS tier
FROM model_perf mp JOIN category_levels cl ON cl.min_perf <= mp.perf
GROUP BY mp.provider, mp.model, mp.category, mp.perf""")

# ---------- 6. task profiles ---------------------------------------------------
con.execute("DROP TABLE IF EXISTS task_profiles")
con.execute("DROP TABLE IF EXISTS task_profile_requirements")
con.execute("CREATE TABLE task_profiles (id VARCHAR PRIMARY KEY, title VARCHAR, created_at TIMESTAMP, "
            "max_consecutive_per_provider INTEGER, max_total_per_provider INTEGER)")
con.execute("CREATE TABLE task_profile_requirements (task_id VARCHAR, category VARCHAR, level INTEGER, PRIMARY KEY (task_id, category))")

PROFILES = {
    'P0_FORE': ("Default foreman: board ops, audit, dispatch, gap-free reports",
                {'agent_tick': 2, 'long_doc': 2, 'debug': 1, 'reasoning': 1, 'delegation': 1,
                 'terminal': 1, 'tool_use': 1, 'guard': 0, 'e2e_vision': -2, 'vision': -3,
                 'creative': -2, 'mock': -3}),
    'P5_VISION_E2E': ("Frontend E2E / visual QA",
                      {'e2e_vision': 4, 'vision': 2, 'terminal': 1, 'debug': 1, 'reasoning': -1,
                       'long_doc': -1, 'creative': -2}),
    'P7_MOCK': ("Mock data / test-loop driving",
                {'mock': 4, 'mechanical': 2, 'code_gen': -1, 'reasoning': -2, 'long_doc': -1,
                 'creative': -2}),
    'P9_REVIEW': ("Code review / security-critical diffs",
                  {'review': 3, 'security': 2, 'code_gen': 1, 'reasoning': 1, 'schema': 1,
                   'mock': -3, 'creative': -2, 'e2e_vision': -2}),
}
for pid, (title, reqs) in PROFILES.items():
    # explicit column list: the two TR-007 diversity columns stay NULL for the
    # seeded profiles (no overrides → global defaults apply; existing behavior)
    con.execute("INSERT INTO task_profiles (id, title, created_at) VALUES (?, ?, now())",
                [pid, title])
    for c, lvl in reqs.items():
        con.execute("INSERT INTO task_profile_requirements VALUES (?,?,?)", [pid, c, lvl])

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
  AND NOT EXISTS (
        SELECT 1 FROM task_profile_requirements rr
        WHERE rr.task_id = r.task_id
          AND NOT EXISTS (SELECT 1 FROM model_tier t
                          WHERE t.provider = m.provider AND t.model = m.model
                            AND t.category = rr.category AND t.tier >= rr.level))""")
con.execute("""
CREATE VIEW v_task_chain AS
SELECT task_id, provider, model, normalized_price, perf_sum, data_class,
       row_number() OVER (PARTITION BY task_id ORDER BY plan_tier ASC,
                          (normalized_price * token_factor) ASC) AS hop
FROM (
  SELECT e.task_id, e.provider, e.model, e.normalized_price, e.token_factor, e.plan_tier, e.data_class,
         (SELECT sum(t.tier) FROM model_tier t
          WHERE t.provider = e.provider AND t.model = e.model) AS perf_sum
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

# ---------- 8. export to namespace + commit ------------------------------------
os.makedirs(f'{NS}/tables', exist_ok=True)
for t in ['level_defs', 'model_perf', 'category_levels', 'model_tier',
          'task_profiles', 'task_profile_requirements']:
    with open(f'{NS}/tables/{t}.jsonl', 'w') as f:
        for row in con.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall():
            f.write(json.dumps(row, default=str) + '\n')
print('exported tables to', NS)
