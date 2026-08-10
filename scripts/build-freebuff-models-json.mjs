#!/usr/bin/env node
// 解析官方 freebuff 源 → 生成 models.json（供 GitHub Releases 兜底）
// 用法: node scripts/build-freebuff-models-json.mjs [输出路径]
// 默认输出: freebuff-models.json（仓库根目录）
//
// 生成的 JSON 结构：
// {
//   "generatedAt": "ISO 时间",
//   "source": "CodebuffAI/freebuff main",
//   "models": [{ id, session, agent, upstream }, ...],   // 动态模型表
//   "pools": { "premium": [...], "glm": [...], "standard": [...] }
// }
//
// 注意：本脚本是 GitHub Actions 用的独立解析器，
// 与 worker.js 内的解析逻辑保持一致（同一个真源）。

import { writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..");

// 与 worker.js 相同的 3 个源（raw 主源 + jsDelivr 备用）
const SOURCES = {
  agents: [
    "https://raw.githubusercontent.com/CodebuffAI/freebuff/main/common/src/constants/free-agents.ts",
    "https://cdn.jsdelivr.net/gh/CodebuffAI/freebuff@main/common/src/constants/free-agents.ts",
  ],
  models: [
    "https://raw.githubusercontent.com/CodebuffAI/freebuff/main/common/src/constants/freebuff-models.ts",
    "https://cdn.jsdelivr.net/gh/CodebuffAI/freebuff@main/common/src/constants/freebuff-models.ts",
  ],
  stableIds: [
    "https://raw.githubusercontent.com/CodebuffAI/freebuff/main/common/src/constants/freebuff-model-ids.ts",
    "https://cdn.jsdelivr.net/gh/CodebuffAI/freebuff@main/common/src/constants/freebuff-model-ids.ts",
  ],
};

// ---- 解析器（与 worker.js 保持一致）----

function parseModelIdConstants(source) {
  const table = {};
  const knownDefaults = { mimoV25: "mimo/mimo-v2.5" };
  const re = /export\s+const\s+([A-Z0-9_]+)\s*=\s*(?:'([^']*)'|"([^"]*)"|([A-Za-z0-9_.]+))/g;
  let m;
  while ((m = re.exec(source)) !== null) {
    const name = m[1];
    const lit = m[2] ?? m[3] ?? "";
    const expr = m[4] ?? "";
    if (lit) table[name] = lit;
    else if (expr) {
      const member = expr.includes(".") ? expr.split(".").pop() : expr;
      if (knownDefaults[member]) table[name] = knownDefaults[member];
      else if (/^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.:/-]+$/.test(expr)) table[name] = expr;
    }
  }
  return table;
}

function parseAgentMapping(source, modelIdConstants) {
  const mapping = {};
  const blockRe = /FREEBUFF_ROOT_AGENT_ID_BY_MODEL[^=]*=\s*\{([^}]*)\}/;
  const blockMatch = blockRe.exec(source);
  if (!blockMatch) return mapping;
  const body = blockMatch[1];
  const lineRe = /\[\s*([A-Z0-9_]+)\s*\]\s*:\s*'([^']+)'/g;
  let m;
  while ((m = lineRe.exec(body)) !== null) {
    const constName = m[1];
    const agentId = m[2];
    const modelId = modelIdConstants[constName];
    if (modelId) mapping[modelId] = agentId;
  }
  return mapping;
}

function parseModelPools(source, modelIdConstants) {
  const premium = new Set();
  const glm = new Set();
  const constValues = new Map();
  const constListRe = /export\s+const\s+([A-Z0-9_]+)\s*=\s*\[([^\]]*)\]\s*as\s*const/g;
  let cm;
  while ((cm = constListRe.exec(source)) !== null) {
    const name = cm[1];
    const items = [];
    const itemRe = /\.\.\.([A-Z0-9_]+)|'([^']*)'|"([^"]*)"|([A-Za-z0-9_]+)/g;
    let im;
    while ((im = itemRe.exec(cm[2])) !== null) {
      const spread = im[1];
      const lit = im[2] ?? im[3];
      const expr = im[4];
      if (spread) items.push(["spread", spread]);
      else if (lit) items.push(["lit", lit]);
      else if (expr && modelIdConstants[expr]) items.push(["lit", modelIdConstants[expr]]);
    }
    constValues.set(name, items);
  }
  const poolRe = /export\s+const\s+(FREEBUFF_WEB_PREMIUM_MODEL_IDS|FREEBUFF_GLM_V52_MODEL_IDS|FREEBUFF_PREMIUM_MODEL_IDS)\s*=\s*\[([^\]]*)\]/g;
  let pm;
  while ((pm = poolRe.exec(source)) !== null) {
    const poolName = pm[1];
    const items = [];
    const itemRe = /\.\.\.([A-Z0-9_]+)|'([^']*)'|"([^"]*)"|([A-Za-z0-9_]+)/g;
    let im;
    while ((im = itemRe.exec(pm[2])) !== null) {
      const spread = im[1];
      const lit = im[2] ?? im[3];
      const expr = im[4];
      if (spread) {
        const expand = (n) => {
          const entries = constValues.get(n) || [];
          for (const [kind, val] of entries) {
            if (kind === "spread") expand(val);
            else items.push(val);
          }
        };
        expand(spread);
      } else if (lit) items.push(lit);
      else if (expr && modelIdConstants[expr]) items.push(modelIdConstants[expr]);
    }
    if (poolName === "FREEBUFF_GLM_V52_MODEL_IDS") {
      for (const id of items) glm.add(id);
    } else {
      for (const id of items) premium.add(id);
    }
  }
  return { premium: [...premium], glm: [...glm] };
}

// ---- 拉取 ----

async function fetchFirst(urls) {
  for (const url of urls) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10000);
      const resp = await fetch(url, { signal: ctrl.signal });
      clearTimeout(timer);
      if (resp.ok) {
        const text = await resp.text();
        if (text && text.length > 100) return text;
      }
    } catch {}
  }
  return null;
}

// ---- 主流程 ----

async function main() {
  const outPath = process.argv[2] || join(REPO_ROOT, "freebuff-models.json");
  const [agentsSrc, modelsSrc, stableIdsSrc] = await Promise.all([
    fetchFirst(SOURCES.agents),
    fetchFirst(SOURCES.models),
    fetchFirst(SOURCES.stableIds),
  ]);
  if (!agentsSrc || !modelsSrc) {
    console.error("❌ 拉取官方源失败（agents 或 models 为空），不生成 JSON");
    process.exit(1);
  }
  try {
    const modelIdConstants = {
      ...parseModelIdConstants(stableIdsSrc || ""),
      ...parseModelIdConstants(modelsSrc),
    };
    const agentMapping = parseAgentMapping(agentsSrc, modelIdConstants);
    if (Object.keys(agentMapping).length === 0) {
      console.error("❌ 解析 agent 映射为空，不生成 JSON");
      process.exit(1);
    }
    const pools = parseModelPools(modelsSrc, modelIdConstants);
    const models = Object.entries(agentMapping).map(([modelId, agentId]) => ({
      id: modelId,
      session: modelId,
      agent: agentId,
      upstream: modelId,
    }));
    const premium = new Set(pools.premium);
    const glm = new Set(pools.glm);
    const standard = models
      .map((m) => m.id)
      .filter((id) => !premium.has(id) && !glm.has(id));
    const payload = {
      generatedAt: new Date().toISOString(),
      source: "CodebuffAI/freebuff main",
      models,
      pools: {
        premium: [...premium],
        glm: [...glm],
        standard,
      },
    };
    writeFileSync(outPath, JSON.stringify(payload, null, 2) + "\n");
    console.log(`✅ 生成 ${outPath}`);
    console.log(`   模型数: ${models.length}`);
    for (const m of models) console.log(`     ${m.id} -> ${m.agent}`);
    console.log(`   premium 池: ${payload.pools.premium.join(", ")}`);
    console.log(`   glm 池: ${payload.pools.glm.join(", ")}`);
    console.log(`   standard 池: ${payload.pools.standard.join(", ")}`);
  } catch (e) {
    console.error("❌ 解析失败:", e.message);
    process.exit(1);
  }
}

main();
