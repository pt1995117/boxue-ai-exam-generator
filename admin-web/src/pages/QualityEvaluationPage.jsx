import React, { useEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { Link } from 'react-router-dom';
import {
  Button,
  Card,
  Col,
  Descriptions,
  Input,
  InputNumber,
  Progress,
  Row,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  getQaConfig,
  getQaDrift,
  getQaOverview,
  getQaOpsWeekly,
  getQaPricing,
  getQaReleaseReport,
  getQaReleases,
  getQaRunDetail,
  getQaThresholds,
  getQaTrends,
  getJudgeTask,
  listMaterials,
  listJudgeTasks,
  listQaAlerts,
  listQaLlmCalls,
  listQaRuns,
  createJudgeTask,
  cancelJudgeTask,
  updateQaAlertStatus,
  updateQaConfig,
  updateQaPricing,
  updateQaThresholds,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const { Text } = Typography;

function pct(v) {
  const n = Number(v || 0);
  return `${(n * 100).toFixed(2)}%`;
}

function displayCurrencyUnit(currency) {
  const c = String(currency || 'CNY').trim().toUpperCase();
  if (c === 'CNY' || c === 'RMB' || c === 'CNH') return '元';
  return c || '元';
}

function formatAmount(value, currency, digits = 4) {
  return `${Number(value || 0).toFixed(digits)} ${displayCurrencyUnit(currency)}`;
}

function avg(arr) {
  if (!Array.isArray(arr) || !arr.length) return 0;
  return arr.reduce((s, x) => s + Number(x || 0), 0) / arr.length;
}

function quantile(arr, q) {
  if (!Array.isArray(arr) || !arr.length) return 0;
  const sorted = arr.map((x) => Number(x || 0)).sort((a, b) => a - b);
  const pos = (sorted.length - 1) * q;
  const base = Math.floor(pos);
  const rest = pos - base;
  const left = sorted[base] ?? sorted[0];
  const right = sorted[base + 1] ?? left;
  return left + (right - left) * rest;
}

function apiErrMsg(e, fallback) {
  const msg = e?.response?.data?.error?.message
    || e?.response?.data?.message
    || e?.message
    || '';
  const status = e?.response?.status;
  const url = e?.config?.url || '';
  return [fallback, status ? `status=${status}` : '', url ? `url=${url}` : '', msg].filter(Boolean).join(' | ');
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function getJudgeBaselineScore(offlineJudge) {
  if (!offlineJudge || typeof offlineJudge !== 'object') return null;
  if (offlineJudge.baseline_score != null) return Number(offlineJudge.baseline_score);
  if (offlineJudge.penalty_score != null) return Number(offlineJudge.penalty_score);
  return null;
}

function toTenPointScore(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  // 兼容历史百分制结果：>10 视为 0-100，按 10 分制展示
  return n > 10 ? n / 10 : n;
}

function judgeDimensionDisplayRows(offlineJudge) {
  const dims = offlineJudge?.dimension_results;
  if (!dims || typeof dims !== 'object') return [];
  const rows = Object.entries(dims).map(([name, dr]) => {
    const status = dr?.status || 'SKIP';
    return {
      name,
      status,
      // 维度分统一以后端 dimension_results.score_10 为准
      score: status === 'SKIP' ? null : (dr?.score_10 != null && dr?.score_10 !== '' ? Number(dr.score_10) : null),
    };
  });
  return rows;
}

// Critic rejection reason type -> display label (for quality evaluation stats)
const CRITIC_FAIL_TYPE_LABELS = {
  no_question: '未生成题目',
  question_type_mismatch: '题型不一致',
  generation_mode: '模式/业务场景不符',
  format_bracket: '括号格式错误',
  material_missing: '材料缺失',
  readability_fail: '可读性不通过',
  duplicate_stem: '重复题干',
  reverse_solve_fail: '反向解题失败',
  answer_mismatch: '答案不一致',
  grounding_fail: '依据/信息不对称',
  quality_fail: '题目质量不合格',
  format_fail: '格式问题',
  explanation_fail: '解析不合格',
  term_lock_fail: '专有名词锁词违规',
  code_check_fail: '计算代码校验不通过',
  leakage_fail: '泄题判定',
  difficulty_out_of_range: '难度超范围',
  writer_issue: '生成者校验问题',
  debug_forced: '(调试)强制失败',
  unknown: '未知',
};

export default function QualityEvaluationPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const loadSeqRef = useRef(0);
  const refreshSeqRef = useRef(0);
  const [loading, setLoading] = useState(false);
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('');
  const [days, setDays] = useState(30);
  const [runs, setRuns] = useState([]);
  const [selectedRunIds, setSelectedRunIds] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [overview, setOverview] = useState({});
  const [runDetail, setRunDetail] = useState({});
  const [llmCalls, setLlmCalls] = useState([]);
  const [trends, setTrends] = useState([]);
  const [thresholds, setThresholds] = useState({});
  const [alerts, setAlerts] = useState([]);
  const [pricing, setPricing] = useState({});
  const [newPricingModel, setNewPricingModel] = useState('');
  const [opsWeekly, setOpsWeekly] = useState({});
  const [driftBase, setDriftBase] = useState('');
  const [driftTarget, setDriftTarget] = useState('');
  const [releaseBaseRunIds, setReleaseBaseRunIds] = useState([]);
  const [driftResult, setDriftResult] = useState(null);
  const [releaseReport, setReleaseReport] = useState(null);
  const [llmQuestionFilter, setLlmQuestionFilter] = useState('');
  const [judgeRunning, setJudgeRunning] = useState(false);
  const [judgeSelectedQuestionIds, setJudgeSelectedQuestionIds] = useState([]);
  const [judgeBatchRunIds, setJudgeBatchRunIds] = useState([]);
  const [judgeBatchQuestionRows, setJudgeBatchQuestionRows] = useState([]);
  const [judgeBatchLoading, setJudgeBatchLoading] = useState(false);
  const [judgeBatchProgress, setJudgeBatchProgress] = useState({ running: false, completed: 0, total: 0, currentRunId: '' });
  const [judgeTaskItems, setJudgeTaskItems] = useState([]);
  const [activeJudgeTaskId, setActiveJudgeTaskId] = useState('');
  const [cancellingJudgeTaskId, setCancellingJudgeTaskId] = useState('');

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    setJudgeBatchProgress({ running: false, completed: 0, total: 0, currentRunId: '' });
    // Tenant switched: reset tenant-scoped filters/selections to avoid stale city data.
    setMaterialVersionId('');
    setSelectedRunIds([]);
    setSelectedRunId('');
    setRuns([]);
    setRunDetail({});
    setLlmCalls([]);
    setDriftBase('');
    setDriftTarget('');
    setReleaseBaseRunIds([]);
    setJudgeTaskItems([]);
    setActiveJudgeTaskId('');
  }, [tenantId]);

  const isJudgeTaskRunning = (status) => ['pending', 'running'].includes(String(status || '').toLowerCase());

  const loadJudgeTaskList = async (tid, keepActive = true) => {
    if (!tid) return;
    const res = await listJudgeTasks(tid, { limit: 100 });
    const items = Array.isArray(res?.items) ? res.items : [];
    setJudgeTaskItems(items);
    if (!items.length) {
      if (!keepActive) setActiveJudgeTaskId('');
      return;
    }
    const running = items.find((x) => isJudgeTaskRunning(x?.status));
    const preferred = String((running?.task_id || items[0]?.task_id || '')).trim();
    if (!keepActive || !activeJudgeTaskId) {
      setActiveJudgeTaskId(preferred);
      return;
    }
    const hasActive = items.some((x) => String(x?.task_id || '') === String(activeJudgeTaskId));
    if (!hasActive) setActiveJudgeTaskId(preferred);
  };

  const loadAll = async () => {
    if (!tenantId) return;
    const seq = ++loadSeqRef.current;
    const tenantSnapshot = tenantId;
    setLoading(true);
    try {
      const mats = await listMaterials(tenantId);
      if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
      const mItems = mats.items || [];
      setMaterials(mItems);
      const effective = mItems.find((x) => x.status === 'effective');
      const defaultMid = materialVersionId || (effective?.material_version_id || '');

      let effectiveMid = materialVersionId || defaultMid || '';
      let runRes = await listQaRuns(tenantId, {
        material_version_id: effectiveMid || undefined,
        days,
        success_only: 1,
        page: 1,
        page_size: 200,
      });
      let runItems = runRes.items || [];
      if (!materialVersionId && effectiveMid && runItems.length === 0) {
        runRes = await listQaRuns(tenantId, {
          days,
          success_only: 1,
          page: 1,
          page_size: 200,
        });
        runItems = runRes.items || [];
        if (runItems.length > 0) {
          if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
          effectiveMid = '';
          message.info('当前生效教材暂无评估数据，已切换为全部教材范围');
        }
      }
      if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
      setRuns(runItems);
      const validRunSet = new Set((runItems || []).map((x) => String(x.run_id || '')).filter(Boolean));
      let scopedRunIds = (selectedRunIds || []).map((x) => String(x || '')).filter((x) => validRunSet.has(x));
      if (scopedRunIds.length === 0) {
        // 默认全选
        scopedRunIds = (runItems || []).map((x) => String(x.run_id || '')).filter(Boolean);
      }
      setSelectedRunIds(scopedRunIds);
      const selectedStillExists = selectedRunId && validRunSet.has(String(selectedRunId));
      const firstRunId = selectedStillExists ? selectedRunId : (scopedRunIds[0] || runItems[0]?.run_id || '');
      if (String(firstRunId || '') !== String(selectedRunId || '')) setSelectedRunId(firstRunId || '');
      // Default baseline: prefer latest release from version management; else qa_config.baseline_run_id; else second-newest run
      const [releasesRes, qaConfig] = await Promise.all([
        getQaReleases(tenantId).catch(() => ({ items: [] })),
        getQaConfig(tenantId).catch(() => ({})),
      ]);
      if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
      const releaseItems = releasesRes?.items || [];
      const latestReleaseRunIds = Array.isArray(releaseItems[0]?.run_ids) && releaseItems[0].run_ids.length
        ? releaseItems[0].run_ids.map((x) => String(x || '').trim()).filter(Boolean)
        : (releaseItems[0]?.run_id ? [String(releaseItems[0].run_id).trim()] : []);
      const latestReleaseRunId = latestReleaseRunIds[0] || '';
      const releaseBaselineExists = latestReleaseRunId && runItems.some((x) => String(x.run_id) === latestReleaseRunId);
      const savedBaseline = String(qaConfig?.baseline_run_id || '').trim();
      const savedBaselineExists = savedBaseline && runItems.some((x) => String(x.run_id) === savedBaseline);
      const driftBaseExists = driftBase && runItems.some((x) => String(x.run_id) === String(driftBase));
      if (!driftBase || !driftBaseExists) {
        setDriftBase(releaseBaselineExists ? latestReleaseRunId : (savedBaselineExists ? savedBaseline : (runItems[1]?.run_id || '')));
      }
      setReleaseBaseRunIds(releaseBaselineExists ? latestReleaseRunIds : []);
      const driftTargetExists = driftTarget && runItems.some((x) => String(x.run_id) === String(driftTarget));
      if ((!driftTarget || !driftTargetExists) && runItems[0]?.run_id) setDriftTarget(runItems[0].run_id);

      const [ov, tr, th, al, pr, wk] = await Promise.all([
        getQaOverview(tenantId, {
          material_version_id: effectiveMid || undefined,
          days,
          run_id: firstRunId || undefined,
          run_ids: scopedRunIds.length ? scopedRunIds.join(',') : undefined,
        }),
        getQaTrends(tenantId, { material_version_id: effectiveMid || undefined, days }),
        getQaThresholds(tenantId),
        listQaAlerts(tenantId, { page: 1, page_size: 200 }),
        getQaPricing(tenantId),
        getQaOpsWeekly(tenantId, { days: 7, run_id: firstRunId || undefined }),
      ]);
      if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
      setOverview(ov || {});
      setTrends((tr.points || []));
      setThresholds(th || {});
      setAlerts(al.items || []);
      setPricing(pr || {});
      setOpsWeekly(wk || {});

      if (firstRunId) {
        const [detail, calls] = await Promise.all([
          getQaRunDetail(tenantId, firstRunId),
          listQaLlmCalls(tenantId, { run_id: firstRunId, question_id: llmQuestionFilter || undefined, page: 1, page_size: 2000 }),
        ]);
        if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
        setRunDetail(detail || {});
        setLlmCalls(calls.items || []);
      } else {
        if (seq !== loadSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
        setRunDetail({});
        setLlmCalls([]);
      }
    } catch (e) {
      if (seq === loadSeqRef.current && tenantSnapshot === getGlobalTenantId()) {
        message.error(apiErrMsg(e, '加载评估数据失败'));
      }
    } finally {
      if (seq === loadSeqRef.current && tenantSnapshot === getGlobalTenantId()) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, days, materialVersionId]);

  useEffect(() => {
    if (!tenantId) return;
    loadJudgeTaskList(tenantId, true).catch(() => setJudgeTaskItems([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, selectedRunId]);

  const onRefreshCurrentRun = async (runId, scopedRunIds = selectedRunIds) => {
    if (!tenantId || !runId) return;
    const seq = ++refreshSeqRef.current;
    const tenantSnapshot = tenantId;
    setSelectedRunId(runId);
    setLoading(true);
    try {
      const [ov, detail, calls] = await Promise.all([
        getQaOverview(tenantId, {
          material_version_id: materialVersionId || undefined,
          run_id: runId,
          run_ids: Array.isArray(scopedRunIds) && scopedRunIds.length ? scopedRunIds.join(',') : undefined,
        }),
        getQaRunDetail(tenantId, runId),
        listQaLlmCalls(tenantId, { run_id: runId, question_id: llmQuestionFilter || undefined, page: 1, page_size: 2000 }),
      ]);
      if (seq !== refreshSeqRef.current || tenantSnapshot !== getGlobalTenantId()) return;
      setOverview(ov || {});
      setRunDetail(detail || {});
      setLlmCalls(calls.items || []);
    } catch (e) {
      if (seq === refreshSeqRef.current && tenantSnapshot === getGlobalTenantId()) {
        message.error(apiErrMsg(e, '加载运行详情失败'));
      }
    } finally {
      if (seq === refreshSeqRef.current && tenantSnapshot === getGlobalTenantId()) {
        setLoading(false);
      }
    }
  };

  const activeJudgeTask = useMemo(
    () => (judgeTaskItems || []).find((x) => String(x?.task_id || '') === String(activeJudgeTaskId || '')) || null,
    [judgeTaskItems, activeJudgeTaskId]
  );

  const onSaveThresholds = async () => {
    if (!tenantId) return;
    try {
      const next = await updateQaThresholds(tenantId, thresholds);
      setThresholds(next || {});
      message.success('阈值已更新');
    } catch (e) {
      message.error(apiErrMsg(e, '更新阈值失败'));
    }
  };

  const onRunDrift = async () => {
    if (!tenantId || !driftBase || !driftTarget) {
      message.warning('请选择基线与目标 run');
      return;
    }
    try {
      const params = { target_run_id: driftTarget };
      if (Array.isArray(releaseBaseRunIds) && releaseBaseRunIds.length > 1) {
        params.base_run_ids = releaseBaseRunIds.join(',');
      } else {
        params.base_run_id = driftBase;
      }
      const res = await getQaDrift(tenantId, params);
      setDriftResult(res || null);
    } catch (e) {
      message.error(apiErrMsg(e, '漂移对比失败'));
    }
  };

  const onRunReleaseReport = async () => {
    if (!tenantId || !driftBase || !driftTarget) {
      message.warning('请选择基线与目标 run');
      return;
    }
    try {
      const params = { target_run_id: driftTarget };
      if (Array.isArray(releaseBaseRunIds) && releaseBaseRunIds.length > 1) {
        params.base_run_ids = releaseBaseRunIds.join(',');
      } else {
        params.base_run_id = driftBase;
      }
      const res = await getQaReleaseReport(tenantId, params);
      setReleaseReport(res || null);
    } catch (e) {
      message.error(apiErrMsg(e, '发布评估失败'));
    }
  };

  const onSetBaseline = async () => {
    if (!tenantId || !driftBase) {
      message.warning('请先选择要设为基线的 run');
      return;
    }
    try {
      await updateQaConfig(tenantId, { baseline_run_id: driftBase });
      message.success(`已将该 run 设为发布基线：${driftBase}`);
    } catch (e) {
      message.error(apiErrMsg(e, '设置基线失败'));
    }
  };

  const onAlertStatus = async (alertId, status) => {
    if (!tenantId || !alertId) return;
    try {
      await updateQaAlertStatus(tenantId, alertId, { status });
      const [al, wk] = await Promise.all([
        listQaAlerts(tenantId, { page: 1, page_size: 200 }),
        getQaOpsWeekly(tenantId, { days: 7, run_id: selectedRunId || undefined }),
      ]);
      setAlerts(al.items || []);
      setOpsWeekly(wk || {});
    } catch (e) {
      message.error(apiErrMsg(e, '更新告警失败'));
    }
  };

  const onSavePricing = async () => {
    if (!tenantId) return;
    try {
      const next = await updateQaPricing(tenantId, pricing);
      setPricing(next || {});
      message.success('成本配置已更新');
    } catch (e) {
      message.error(apiErrMsg(e, '更新成本配置失败'));
    }
  };

  const onAddPricingModel = () => {
    const model = String(newPricingModel || '').trim();
    if (!model) {
      message.warning('请先输入模型名');
      return;
    }
    setPricing((prev) => {
      const prevModels = (prev && typeof prev === 'object' && prev.models && typeof prev.models === 'object')
        ? prev.models
        : {};
      if (prevModels[model]) return prev;
      return {
        ...(prev || {}),
        models: {
          ...prevModels,
          [model]: {
            prompt_per_1k: Number(prev?.default_prompt_per_1k || 0),
            completion_per_1k: Number(prev?.default_completion_per_1k || 0),
          },
        },
      };
    });
    setNewPricingModel('');
  };

  const questionRows = useMemo(() => runDetail?.questions || [], [runDetail]);
  const judgeJob = runDetail?.judge_job || {};
  const activeJudgeTaskRunning = isJudgeTaskRunning(activeJudgeTask?.status);
  const persistedJudgeRunning = String(judgeJob?.status || '').toLowerCase() === 'running';
  const judgeBusy = judgeRunning || persistedJudgeRunning || activeJudgeTaskRunning;
  const judgeCompleted = Number(
    activeJudgeTask?.progress?.current != null
      ? activeJudgeTask.progress.current
      : (judgeJob.completed_count || 0)
  );
  const judgeTotal = Number(
    activeJudgeTask?.progress?.total != null
      ? activeJudgeTask.progress.total
      : (judgeJob.requested_count || 0)
  );
  const judgePercent = judgeTotal > 0 ? Math.max(0, Math.min(100, Math.round((judgeCompleted * 100) / judgeTotal))) : 0;
  const batchJudgePercent = judgeBatchProgress.total > 0
    ? Math.max(0, Math.min(100, Math.round((judgeBatchProgress.completed * 100) / judgeBatchProgress.total)))
    : 0;

  useEffect(() => {
    if (!tenantId || !activeJudgeTaskId || !activeJudgeTaskRunning) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const res = await getJudgeTask(tenantId, activeJudgeTaskId);
        const task = res?.task || {};
        setJudgeTaskItems((prev) => {
          const arr = Array.isArray(prev) ? [...prev] : [];
          const idx = arr.findIndex((x) => String(x?.task_id || '') === String(task?.task_id || ''));
          if (idx >= 0) arr[idx] = { ...arr[idx], ...task };
          else arr.unshift(task);
          return arr;
        });
        const taskRunning = isJudgeTaskRunning(task?.status);
        const sameRun = String(task?.run_id || '') === String(selectedRunId || '');
        // 运行中也刷新 run 明细，确保每完成一题都能在页面看到 offline_judge 结果
        if (sameRun && selectedRunId) {
          await onRefreshCurrentRun(selectedRunId);
        }
        if (!taskRunning) {
          await loadJudgeTaskList(tenantId, true);
        }
      } catch (_e) {
        // ignore transient polling errors
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [tenantId, activeJudgeTaskId, activeJudgeTaskRunning, selectedRunId]);

  /** Saved (落库) questions only for Judge multi-select. */
  const savedQuestionRows = useMemo(() => {
    const list = runDetail?.questions || [];
    return list.filter((q) => q.saved === true);
  }, [runDetail?.questions]);
  const judgeSelectionIsSavedOnly = useMemo(() => {
    return true;
  }, []);

  // Clear Judge selection when run changes
  useEffect(() => {
    setJudgeSelectedQuestionIds([]);
  }, [selectedRunId]);

  // Keep batch-run selection valid when run list changes
  useEffect(() => {
    const valid = new Set((runs || []).map((r) => String(r.run_id || '')));
    setJudgeBatchRunIds((prev) => (prev || []).filter((rid) => valid.has(String(rid || ''))));
  }, [runs]);

  // When multiple runs are selected for Judge, load each run's questions for combined display
  useEffect(() => {
    let cancelled = false;
    const loadBatchQuestions = async () => {
      const runIds = Array.from(new Set((judgeBatchRunIds || []).map((x) => String(x || '').trim()).filter(Boolean)));
      if (!tenantId || runIds.length === 0) {
        setJudgeBatchQuestionRows([]);
        return;
      }
      setJudgeBatchLoading(true);
      try {
        const details = await Promise.all(
          runIds.map(async (rid) => {
            try {
              const detail = await getQaRunDetail(tenantId, rid);
              return { rid, detail };
            } catch (e) {
              return { rid, error: apiErrMsg(e, '加载 run 详情失败') };
            }
          })
        );
        if (cancelled) return;
        const failed = details.filter((x) => x.error);
        if (failed.length) {
          message.warning(`部分 run 题目加载失败：${failed.slice(0, 2).map((x) => x.rid).join('，')}${failed.length > 2 ? '...' : ''}`);
        }
        const rows = [];
        for (const item of details) {
          if (!item?.detail) continue;
          const list = item.detail?.questions || [];
          const withSaved = list.filter((q) => q?.saved === true);
          const source = withSaved.length > 0 ? withSaved : list;
          source.forEach((q, idx) => {
            rows.push({
              ...q,
              __run_id: item.rid,
              __run_task_name: item.detail?.task_name || '',
              __row_key: `${item.rid}::${q?.question_id || idx}`,
            });
          });
        }
        setJudgeBatchQuestionRows(rows);
      } finally {
        if (!cancelled) setJudgeBatchLoading(false);
      }
    };
    loadBatchQuestions();
    return () => {
      cancelled = true;
    };
  }, [tenantId, judgeBatchRunIds]);

  useEffect(() => {
    if (!tenantId || !selectedRunId || !persistedJudgeRunning) return undefined;
    const timer = window.setInterval(() => {
      onRefreshCurrentRun(selectedRunId);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [tenantId, selectedRunId, persistedJudgeRunning]);

  const callAggByNode = useMemo(() => {
    const m = new Map();
    for (const c of llmCalls) {
      const key = `${c.node || 'unknown'}|${c.model || 'unknown'}`;
      const cur = m.get(key) || { node: c.node || 'unknown', model: c.model || 'unknown', calls: 0, tokens: 0, latency_ms: 0, errors: 0 };
      cur.calls += 1;
      cur.tokens += Number(c.total_tokens || 0);
      cur.latency_ms += Number(c.latency_ms || 0);
      if (!c.success) cur.errors += 1;
      m.set(key, cur);
    }
    return Array.from(m.values());
  }, [llmCalls]);

  const healthCards = useMemo(() => {
    const latencies = llmCalls.map((x) => Number(x.latency_ms || 0));
    const totalCalls = llmCalls.length;
    const errCalls = llmCalls.filter((x) => !x.success).length;
    const totalTokens = llmCalls.reduce((s, x) => s + Number(x.total_tokens || 0), 0);
    const successRate = totalCalls ? (totalCalls - errCalls) / totalCalls : 0;
    return {
      totalCalls,
      errCalls,
      successRate,
      p50: quantile(latencies, 0.5),
      p95: quantile(latencies, 0.95),
      avgLatency: avg(latencies),
      totalTokens,
      avgTokensPerCall: totalCalls ? totalTokens / totalCalls : 0,
    };
  }, [llmCalls]);

  const judgeResourceCards = useMemo(() => {
    const rows = (runDetail?.questions || [])
      .map((q) => q?.offline_judge)
      .filter((oj) => oj && !oj.error);
    let totalCalls = 0;
    let failedCalls = 0;
    let totalTokens = 0;
    let totalLatency = 0;
    let totalCostUsd = 0;
    for (const oj of rows) {
      const obs = oj?.observability || {};
      const tok = obs?.tokens || {};
      const costs = oj?.costs || {};
      totalCalls += Number(obs.llm_calls || 0);
      failedCalls += Number(obs.failed_calls || 0);
      totalTokens += Number(tok.total_tokens || 0);
      totalLatency += Number(obs.latency_ms || 0);
      totalCostUsd += Number(costs.per_question_usd || 0);
    }
    const qCount = rows.length;
    return {
      questionCount: qCount,
      totalCalls,
      failedCalls,
      totalTokens,
      totalLatency,
      totalCostUsd,
      avgTokensPerQuestion: qCount ? totalTokens / qCount : 0,
      avgLatencyPerQuestion: qCount ? totalLatency / qCount : 0,
      avgCostUsdPerQuestion: qCount ? totalCostUsd / qCount : 0,
    };
  }, [runDetail?.questions]);

  const hardGateFailTop = useMemo(() => {
    const m = new Map();
    for (const q of questionRows) {
      for (const rule of (q.hard_gate?.failed_rules || [])) {
        const k = String(rule || 'unknown');
        m.set(k, Number(m.get(k) || 0) + 1);
      }
    }
    return Array.from(m.entries())
      .map(([rule, count]) => ({ rule, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 20);
  }, [questionRows]);

  const riskDist = useMemo(() => {
    const m = new Map([['low', 0], ['medium', 0], ['high', 0]]);
    for (const q of questionRows) {
      const k = String(q.risk?.level || 'low');
      m.set(k, Number(m.get(k) || 0) + 1);
    }
    return ['low', 'medium', 'high'].map((k) => ({ level: k, count: Number(m.get(k) || 0) }));
  }, [questionRows]);

  const errorByNode = useMemo(() => {
    return callAggByNode
      .map((x) => ({ ...x, error_rate: x.calls ? x.errors / x.calls : 0 }))
      .filter((x) => x.errors > 0)
      .sort((a, b) => b.errors - a.errors);
  }, [callAggByNode]);

  const tokenHotspot = useMemo(() => {
    return [...callAggByNode].sort((a, b) => Number(b.tokens || 0) - Number(a.tokens || 0)).slice(0, 10);
  }, [callAggByNode]);

  const judgeBatchRunOptions = useMemo(() => {
    return (runs || [])
      .filter((r) => Number(r?.saved_count || 0) > 0)
      .map((r) => {
        const name = (r.task_name || '').trim() || r.task_id || r.run_id;
        return {
          value: r.run_id,
          label: `${name} | ${r.run_id} | saved=${r.saved_count} | ${r.ended_at || ''}`,
        };
      });
  }, [runs]);

  const judgeTaskOptions = useMemo(() => {
    return (judgeTaskItems || []).map((t) => {
      const tid = String(t?.task_id || '');
      const runId = String(t?.run_id || '');
      const status = String(t?.status || '');
      const cur = Number(t?.progress?.current || 0);
      const total = Number(t?.progress?.total || 0);
      const title = String(t?.task_name || '').trim() || tid;
      return {
        value: tid,
        label: `${title} | ${runId} | ${status}${total > 0 ? ` ${cur}/${total}` : ''}`,
      };
    });
  }, [judgeTaskItems]);

  const isBatchQuestionView = judgeBatchRunIds.length > 0;
  const judgeTableRows = isBatchQuestionView ? judgeBatchQuestionRows : savedQuestionRows;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Card style={{ width: '100%' }}>
        <Space wrap align="start">
          <Select
            style={{ width: 280 }}
            value={materialVersionId || undefined}
            placeholder="教材版本"
            options={materials.map((m) => ({ label: `${m.material_version_id}${m.status === 'effective' ? '（生效）' : ''}`, value: m.material_version_id }))}
            onChange={setMaterialVersionId}
            allowClear
          />
          <InputNumber min={1} max={365} value={days} onChange={(v) => setDays(Number(v || 30))} addonBefore="最近天数" />
          <Button onClick={loadAll} loading={loading}>刷新</Button>
          <Select
            mode="multiple"
            style={{ width: 520 }}
            value={selectedRunIds}
            placeholder="按任务名称选择 run（支持全选）"
            options={runs.map((r) => {
              const name = (r.task_name || '').trim() || (r.task_id || '') || r.run_id;
              const date = (r.ended_at || '').slice(0, 10);
              const label = date ? `${name} | ${date}` : name;
              return { label, value: r.run_id };
            })}
            onChange={(vals) => {
              const picked = Array.from(new Set((vals || []).map((x) => String(x || '')).filter(Boolean)));
              const next = picked.length > 0 ? picked : (runs || []).map((r) => String(r.run_id || '')).filter(Boolean);
              setSelectedRunIds(next);
              const nextRunId = next.includes(String(selectedRunId || '')) ? String(selectedRunId || '') : (next[0] || '');
              if (nextRunId) onRefreshCurrentRun(nextRunId, next);
            }}
            showSearch
            optionFilterProp="label"
            filterOption={(input, option) =>
              (option?.label ?? '').toLowerCase().includes((input || '').toLowerCase())
            }
          />
          <Button
            onClick={() => {
              const allRunIds = (runs || []).map((r) => String(r.run_id || '')).filter(Boolean);
              setSelectedRunIds(allRunIds);
              const nextRunId = allRunIds.includes(String(selectedRunId || '')) ? String(selectedRunId || '') : (allRunIds[0] || '');
              if (nextRunId) onRefreshCurrentRun(nextRunId, allRunIds);
            }}
            disabled={!runs.length}
          >
            全选
          </Button>
          <Text type="secondary">已选 {selectedRunIds.length}/{runs.length}</Text>
        </Space>
      </Card>

      <Tabs
        style={{ width: '100%' }}
        items={[
          {
            key: 'overview',
            label: '总览',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Row gutter={12}>
                  {[
                    ['run_count', '统计run数', Number(overview.run_count || 0)],
                    ['hard_pass_rate', '入库率', pct(overview.hard_pass_rate)],
                    ['logic_pass_rate', '逻辑合格率', pct(overview.logic_pass_rate)],
                    ['risk_high_rate', 'Critic判失败率', pct(overview.risk_high_rate)],
                    ['quality_score_avg', '质量均分(离线Judge)', Number(overview.quality_score_avg || 0).toFixed(2)],
                    ['judge_pass_rate', 'Judge通过率', overview.judge_pass_rate != null ? pct(overview.judge_pass_rate) : '—'],
                    ['judge_review_rate', 'Judge复核率', overview.judge_review_rate != null ? pct(overview.judge_review_rate) : '—'],
                    ['judge_baseline_score_avg', 'Judge基线均分', overview.judge_baseline_score_avg != null ? Number(overview.judge_baseline_score_avg).toFixed(2) : '—'],
                    ['judge_overall_score_avg', 'Judge均分', overview.judge_overall_score_avg != null ? Number(overview.judge_overall_score_avg).toFixed(2) : '—'],
                    ['judge_reject_rate', 'Judge拒绝率', overview.judge_reject_rate != null ? pct(overview.judge_reject_rate) : '—'],
                    ['judge_scored_count', 'Judge已评分题数', Number(overview.judge_scored_count || 0)],
                    ['judge_pass_count', 'Judge通过题数', Number(overview.judge_pass_count || 0)],
                    ['judge_review_count', 'Judge复核题数', Number(overview.judge_review_count || 0)],
                    ['judge_reject_count', 'Judge拒绝题数', Number(overview.judge_reject_count || 0)],
                    ['duplicate_rate', '重复率', pct(overview.duplicate_rate)],
                    ['knowledge_match_rate', '考点命中率', pct(overview.knowledge_match_rate)],
                    ['avg_tokens_per_question', '出题平均Token/题', Number(overview.avg_tokens_per_question || 0).toFixed(2)],
                    ['avg_latency_ms_per_question', '出题平均时长ms/题', Number(overview.avg_latency_ms_per_question || 0).toFixed(2)],
                    ['avg_cost_per_question', '出题平均成本/题（毛）', formatAmount(overview.avg_cost_per_question, overview.currency)],
                    ['cpvq', 'CPVQ（单题有效成本）', overview.cpvq != null ? formatAmount(overview.cpvq, overview.currency) : '—'],
                    ['total_cost', '总成本', formatAmount(overview.total_cost, overview.currency)],
                    ['judge_avg_tokens_per_question', 'Judge平均Token/题', Number(overview.judge_avg_tokens_per_question || 0).toFixed(2)],
                    ['judge_avg_latency_ms_per_question', 'Judge平均时长ms/题', Number(overview.judge_avg_latency_ms_per_question || 0).toFixed(2)],
                    ['judge_avg_cost_usd_per_question', 'Judge平均成本/题(USD)', Number(overview.judge_avg_cost_usd_per_question || 0).toFixed(6)],
                  ].map((x) => (
                    <Col xs={24} md={12} lg={6} key={x[0]} style={{ marginBottom: 12 }}>
                      <Card loading={loading}>
                        <Text type="secondary">{x[1]}</Text>
                        <div style={{ fontSize: 24, fontWeight: 600 }}>{x[2]}</div>
                      </Card>
                    </Col>
                  ))}
                </Row>
                <Card size="small" title="切片出题成功率（critic最终通过并落库）">
                  <Table
                    size="small"
                    rowKey={(r) => `${r.slice_id}|${r.slice_path || ''}`}
                    dataSource={Array.isArray(overview?.slice_success_stats) ? overview.slice_success_stats : []}
                    pagination={{ pageSize: 10 }}
                    columns={[
                      { title: '切片ID', dataIndex: 'slice_id', width: 100 },
                      { title: '来源路径', dataIndex: 'slice_path', ellipsis: true },
                      { title: '尝试次数', dataIndex: 'attempt_count', width: 110 },
                      { title: '成功次数', dataIndex: 'success_count', width: 110 },
                      { title: '成功率', dataIndex: 'success_rate', width: 120, render: (v) => pct(v) },
                    ]}
                  />
                </Card>
              </Space>
            ),
          },
          {
            key: 'cost_board',
            label: '金额成本',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Card size="small" title="成本概览">
                  <Descriptions size="small" bordered column={2}>
                    <Descriptions.Item label="currency">{displayCurrencyUnit(overview.currency)}</Descriptions.Item>
                    <Descriptions.Item label="出题 avg_cost_per_question">{Number(overview.avg_cost_per_question || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="cpvq">{runDetail?.batch_metrics?.cpvq != null ? Number(runDetail.batch_metrics.cpvq).toFixed(6) : '—'}</Descriptions.Item>
                    <Descriptions.Item label="出题 total_cost">{Number(overview.total_cost || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="出题 avg_cost_per_call">{Number((runDetail?.batch_metrics || {}).avg_cost_per_call || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="Judge total_cost_usd">{Number((runDetail?.batch_metrics || {}).judge_total_cost_usd || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="Judge avg_cost_usd_per_question">{Number((runDetail?.batch_metrics || {}).judge_avg_cost_usd_per_question || 0).toFixed(6)}</Descriptions.Item>
                  </Descriptions>
                </Card>
                <Row gutter={12}>
                  <Col xs={24} xl={12}>
                    <Card size="small" title="按模型成本">
                      <Table
                        size="small"
                        rowKey={(r) => r.model}
                        dataSource={Object.entries((runDetail?.cost_summary || {}).by_model || {}).map(([model, cost]) => ({ model, cost }))}
                        pagination={false}
                        columns={[
                          { title: 'model', dataIndex: 'model' },
                          { title: 'cost', dataIndex: 'cost', width: 160, render: (v) => Number(v || 0).toFixed(6) },
                        ]}
                      />
                    </Card>
                  </Col>
                  <Col xs={24} xl={12}>
                    <Card size="small" title="按节点成本">
                      <Table
                        size="small"
                        rowKey={(r) => r.node}
                        dataSource={Object.entries((runDetail?.cost_summary || {}).by_node || {}).map(([node, cost]) => ({ node, cost }))}
                        pagination={false}
                        columns={[
                          { title: 'node', dataIndex: 'node' },
                          { title: 'cost', dataIndex: 'cost', width: 160, render: (v) => Number(v || 0).toFixed(6) },
                        ]}
                      />
                    </Card>
                  </Col>
                </Row>
                <Card size="small" title="成本单价配置（每1k token）">
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Descriptions size="small" bordered column={2}>
                      <Descriptions.Item label="currency">{displayCurrencyUnit(pricing.currency)}</Descriptions.Item>
                      <Descriptions.Item label="default_prompt_per_1k">{Number(pricing.default_prompt_per_1k || 0).toFixed(6)}</Descriptions.Item>
                      <Descriptions.Item label="default_completion_per_1k">{Number(pricing.default_completion_per_1k || 0).toFixed(6)}</Descriptions.Item>
                    </Descriptions>
                    <Space wrap>
                      <Input
                        style={{ width: 320 }}
                        placeholder="新增模型名（如 gpt-5 / gpt-4o）"
                        value={newPricingModel}
                        onChange={(e) => setNewPricingModel(e.target.value)}
                        onPressEnter={onAddPricingModel}
                      />
                      <Button onClick={onAddPricingModel}>新增模型</Button>
                    </Space>
                    <Table
                      size="small"
                      rowKey={(r) => r.model}
                      dataSource={Object.entries(pricing.models || {}).map(([model, cfg]) => ({ model, ...(cfg || {}) }))}
                      pagination={false}
                      columns={[
                        { title: 'model', dataIndex: 'model' },
                        {
                          title: 'prompt_per_1k',
                          dataIndex: 'prompt_per_1k',
                          render: (_, r) => (
                            <InputNumber
                              step={0.001}
                              value={Number((pricing.models?.[r.model] || {}).prompt_per_1k || 0)}
                              onChange={(v) => setPricing((prev) => ({
                                ...(prev || {}),
                                models: { ...(prev.models || {}), [r.model]: { ...(prev.models?.[r.model] || {}), prompt_per_1k: Number(v || 0) } },
                              }))}
                            />
                          ),
                        },
                        {
                          title: 'completion_per_1k',
                          dataIndex: 'completion_per_1k',
                          render: (_, r) => (
                            <InputNumber
                              step={0.001}
                              value={Number((pricing.models?.[r.model] || {}).completion_per_1k || 0)}
                              onChange={(v) => setPricing((prev) => ({
                                ...(prev || {}),
                                models: { ...(prev.models || {}), [r.model]: { ...(prev.models?.[r.model] || {}), completion_per_1k: Number(v || 0) } },
                              }))}
                            />
                          ),
                        },
                      ]}
                    />
                    <Button type="primary" onClick={onSavePricing}>保存成本配置</Button>
                  </Space>
                </Card>
              </Space>
            ),
          },
          {
            key: 'system_health',
            label: '系统健康',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Row gutter={12}>
                  {[
                    ['total_calls', '出题总调用数', healthCards.totalCalls],
                    ['error_calls', '出题错误调用数', healthCards.errCalls],
                    ['success_rate', '出题调用成功率', pct(healthCards.successRate)],
                    ['p95_latency', 'P95时延(ms)', Number(healthCards.p95 || 0).toFixed(2)],
                    ['avg_latency', '平均时延(ms)', Number(healthCards.avgLatency || 0).toFixed(2)],
                    ['total_tokens', '出题总Token', healthCards.totalTokens],
                    ['avg_tokens_call', '出题平均Token/调用', Number(healthCards.avgTokensPerCall || 0).toFixed(2)],
                  ].map((x) => (
                    <Col xs={24} md={12} lg={6} key={x[0]} style={{ marginBottom: 12 }}>
                      <Card loading={loading}>
                        <Text type="secondary">{x[1]}</Text>
                        <div style={{ fontSize: 24, fontWeight: 600 }}>{x[2]}</div>
                      </Card>
                    </Col>
                  ))}
                </Row>
                <Row gutter={12}>
                  <Col xs={24} xl={12}>
                    <Card size="small" title="门禁失败分布 Top">
                      <Table
                        size="small"
                        rowKey={(r) => r.rule}
                        dataSource={hardGateFailTop}
                        pagination={false}
                        columns={[
                          { title: 'failed_rule', dataIndex: 'rule' },
                          { title: 'count', dataIndex: 'count', width: 90 },
                        ]}
                      />
                    </Card>
                  </Col>
                  <Col xs={24} xl={12}>
                    <Card size="small" title="风险等级分布">
                      <Table
                        size="small"
                        rowKey={(r) => r.level}
                        dataSource={riskDist}
                        pagination={false}
                        columns={[
                          { title: 'level', dataIndex: 'level', width: 120, render: (v) => <Tag color={v === 'high' ? 'red' : v === 'medium' ? 'orange' : 'green'}>{v}</Tag> },
                          { title: 'count', dataIndex: 'count', width: 90 },
                        ]}
                      />
                    </Card>
                  </Col>
                </Row>
                <Row gutter={12}>
                  <Col xs={24} xl={12}>
                    <Card size="small" title="错误集中度（按节点）">
                      <Table
                        size="small"
                        rowKey={(r) => `${r.node}|${r.model}`}
                        dataSource={errorByNode}
                        pagination={false}
                        columns={[
                          { title: 'node', dataIndex: 'node' },
                          { title: 'model', dataIndex: 'model' },
                          { title: 'errors', dataIndex: 'errors', width: 80 },
                          { title: 'calls', dataIndex: 'calls', width: 80 },
                          { title: 'error_rate', dataIndex: 'error_rate', width: 110, render: (v) => pct(v) },
                        ]}
                      />
                    </Card>
                  </Col>
                  <Col xs={24} xl={12}>
                    <Card size="small" title="成本热点（按节点）">
                      <Table
                        size="small"
                        rowKey={(r) => `${r.node}|${r.model}`}
                        dataSource={tokenHotspot}
                        pagination={false}
                        columns={[
                          { title: 'node', dataIndex: 'node' },
                          { title: 'model', dataIndex: 'model' },
                          { title: 'tokens', dataIndex: 'tokens', width: 110 },
                          { title: 'latency(ms)', dataIndex: 'latency_ms', width: 120, render: (v) => Number(v || 0).toFixed(2) },
                        ]}
                      />
                    </Card>
                  </Col>
                </Row>
                <Card size="small" title="离线 Judge 资源消耗（独立）">
                  <Descriptions size="small" bordered column={2}>
                    <Descriptions.Item label="Judge 题目数">{judgeResourceCards.questionCount}</Descriptions.Item>
                    <Descriptions.Item label="Judge 总调用数">{judgeResourceCards.totalCalls}</Descriptions.Item>
                    <Descriptions.Item label="Judge 失败调用数">{judgeResourceCards.failedCalls}</Descriptions.Item>
                    <Descriptions.Item label="Judge 总Token">{Math.round(judgeResourceCards.totalTokens)}</Descriptions.Item>
                    <Descriptions.Item label="Judge 总时延(ms)">{Math.round(judgeResourceCards.totalLatency)}</Descriptions.Item>
                    <Descriptions.Item label="Judge 总成本(USD)">{Number(judgeResourceCards.totalCostUsd || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="Judge 平均Token/题">{Number(judgeResourceCards.avgTokensPerQuestion || 0).toFixed(2)}</Descriptions.Item>
                    <Descriptions.Item label="Judge 平均时延(ms)/题">{Number(judgeResourceCards.avgLatencyPerQuestion || 0).toFixed(2)}</Descriptions.Item>
                    <Descriptions.Item label="Judge 平均成本(USD)/题">{Number(judgeResourceCards.avgCostUsdPerQuestion || 0).toFixed(6)}</Descriptions.Item>
                  </Descriptions>
                </Card>
              </Space>
            ),
          },
          {
            key: 'question',
            label: '单题评估',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text type="secondary">
                  单题评估（Judge）仅在本页手动点击后执行；出题过程中与出题结束后都不会自动触发。
                </Typography.Text>
                {selectedRunId && (
                  <Space wrap align="center">
                    <Select
                      mode="multiple"
                      style={{ width: 560 }}
                      placeholder="批量选择 run 并执行 Judge（仅含 saved_count>0）"
                      value={judgeBatchRunIds}
                      onChange={(vals) => setJudgeBatchRunIds(vals || [])}
                      options={judgeBatchRunOptions}
                      showSearch
                      optionFilterProp="label"
                    />
                    <Button
                      onClick={async () => {
                        if (!tenantId || !judgeBatchRunIds.length) return;
                        const runIds = Array.from(new Set((judgeBatchRunIds || []).map((x) => String(x || '').trim()).filter(Boolean)));
                        if (!runIds.length) return;
                        setJudgeRunning(true);
                        setJudgeBatchProgress({ running: true, completed: 0, total: runIds.length, currentRunId: runIds[0] || '' });
                        let done = 0;
                        let fail = 0;
                        const failMsgs = [];
                        let lastTaskId = '';
                        try {
                          for (let i = 0; i < runIds.length; i += 1) {
                            const rid = runIds[i];
                            if (tenantId !== getGlobalTenantId()) {
                              throw new Error('城市已切换，停止当前批量 Judge');
                            }
                            setJudgeBatchProgress({ running: true, completed: i, total: runIds.length, currentRunId: rid });
                            try {
                              const res = await createJudgeTask(tenantId, {
                                run_id: rid,
                                task_name: `Judge-${rid.slice(0, 18)}`,
                              });
                              const taskId = String(res?.task?.task_id || '');
                              if (taskId) lastTaskId = taskId;
                              if (!taskId) {
                                throw new Error('创建任务成功但未返回 task_id');
                              }
                              setActiveJudgeTaskId(taskId);
                              // 串行执行：当前 run 的 Judge 结束后再进入下一个 run
                              // eslint-disable-next-line no-await-in-loop
                              while (true) {
                                // eslint-disable-next-line no-await-in-loop
                                await sleep(3000);
                                if (tenantId !== getGlobalTenantId()) {
                                  throw new Error('城市已切换，停止当前批量 Judge');
                                }
                                // eslint-disable-next-line no-await-in-loop
                                const detail = await getJudgeTask(tenantId, taskId);
                                const task = detail?.task || {};
                                const status = String(task?.status || '').toLowerCase();
                                const progress = task?.progress || {};
                                setJudgeBatchProgress({
                                  running: true,
                                  completed: i,
                                  total: runIds.length,
                                  currentRunId: `${rid} ${Number(progress.current || 0)}/${Number(progress.total || 0)}`,
                                });
                                if (!isJudgeTaskRunning(status)) {
                                  if (status !== 'completed') {
                                    fail += 1;
                                    failMsgs.push(`${rid}: 任务状态=${status}${task?.errors?.[0] ? `, ${task.errors[0]}` : ''}`);
                                  } else {
                                    done += 1;
                                  }
                                  break;
                                }
                              }
                            } catch (e) {
                              fail += 1;
                              failMsgs.push(`${rid}: ${apiErrMsg(e, '创建 Judge 任务失败')}`);
                            }
                          }
                          setJudgeBatchProgress({ running: true, completed: runIds.length, total: runIds.length, currentRunId: '' });
                          const content = fail === 0
                            ? `批量 Judge 已完成：${done}/${runIds.length}`
                            : `批量 Judge 已完成：成功 ${done}，失败 ${fail}`;
                          if (fail === 0) {
                            message.success(content);
                          } else {
                            message.warning(content);
                            message.error(failMsgs.slice(0, 3).join(' ; '));
                          }
                          await loadJudgeTaskList(tenantId, true);
                          if (lastTaskId) {
                            setActiveJudgeTaskId(lastTaskId);
                          }
                        } catch (e) {
                          message.warning(apiErrMsg(e, '批量 Judge 已中断'));
                        } finally {
                          setJudgeRunning(false);
                          setJudgeBatchProgress((prev) => ({ ...prev, running: false, currentRunId: '' }));
                        }
                      }}
                      loading={judgeRunning}
                      disabled={judgeRunning || judgeBatchRunIds.length === 0}
                    >
                      批量运行 Judge（按 run）
                    </Button>
                    <Button
                      type="primary"
                      loading={judgeBusy}
                      disabled={judgeBusy || isBatchQuestionView}
                      onClick={async () => {
                        if (!tenantId || !selectedRunId) {
                          message.warning('请先选择一个 run');
                          return;
                        }
                        const allQuestionIds = (savedQuestionRows || []).map((r) => String(r?.question_id || '')).filter(Boolean);
                        const selectedIds = (judgeSelectedQuestionIds || []).map((x) => String(x || '')).filter(Boolean);
                        const targetIds = selectedIds.length > 0 ? selectedIds : allQuestionIds;
                        if (targetIds.length === 0) {
                          message.warning('当前 run 没有可执行 Judge 的落库题（saved=true）');
                          return;
                        }
                        setJudgeRunning(true);
                        flushSync(() => {
                          setOverview((prev) => ({
                            ...prev,
                            judge_pass_rate: undefined,
                            judge_baseline_score_avg: undefined,
                            judge_overall_score_avg: undefined,
                            judge_reject_rate: undefined,
                          }));
                          setRunDetail((prev) => {
                            const questions = (prev?.questions || []).map((q) => {
                              const { offline_judge, ...rest } = q || {};
                              return rest;
                            });
                            const bm = prev?.batch_metrics ? { ...prev.batch_metrics } : {};
                            delete bm.judge_pass_count;
                            delete bm.judge_review_count;
                            delete bm.judge_reject_count;
                            delete bm.judge_pass_rate;
                            delete bm.judge_reject_rate;
                            delete bm.judge_baseline_score_avg;
                            delete bm.judge_overall_score_avg;
                            delete bm.quality_score_avg;
                            return { ...prev, questions, batch_metrics: bm };
                          });
                        });
                        try {
                          const body = { question_ids: targetIds };
                          const res = await createJudgeTask(tenantId, {
                            run_id: selectedRunId,
                            question_ids: body.question_ids,
                            task_name: `Judge-${String(selectedRunId).slice(0, 18)}`,
                          });
                          const taskId = String(res?.task?.task_id || '');
                          if (taskId) setActiveJudgeTaskId(taskId);
                          await loadJudgeTaskList(tenantId, true);
                          message.success(
                            taskId
                              ? `Judge 任务已创建：${taskId}（${targetIds.length}题）`
                              : `Judge 任务已创建（${targetIds.length}题）`
                          );
                        } catch (e) {
                          message.error(apiErrMsg(e, '创建 Judge 任务失败'));
                        } finally {
                          setJudgeRunning(false);
                        }
                      }}
                    >
                      开始 Judge 测评
                    </Button>
                    <Select
                      style={{ width: 520 }}
                      placeholder="选择 Judge 任务"
                      value={activeJudgeTaskId || undefined}
                      onChange={(v) => setActiveJudgeTaskId(String(v || ''))}
                      options={judgeTaskOptions}
                      allowClear
                      showSearch
                      optionFilterProp="label"
                    />
                    <Button onClick={() => loadJudgeTaskList(tenantId, true)}>刷新任务</Button>
                    {activeJudgeTaskId && isJudgeTaskRunning(activeJudgeTask?.status) ? (
                      <Button
                        danger
                        loading={cancellingJudgeTaskId === activeJudgeTaskId}
                        onClick={async () => {
                          if (!tenantId || !activeJudgeTaskId) return;
                          setCancellingJudgeTaskId(activeJudgeTaskId);
                          try {
                            await cancelJudgeTask(tenantId, activeJudgeTaskId);
                            message.success('已请求取消 Judge 任务');
                            await loadJudgeTaskList(tenantId, true);
                          } catch (e) {
                            message.error(apiErrMsg(e, '取消 Judge 任务失败'));
                          } finally {
                            setCancellingJudgeTaskId('');
                          }
                        }}
                      >
                        取消任务
                      </Button>
                    ) : null}
                    {isBatchQuestionView ? (
                      <Typography.Text type="secondary">批量 run 视图下仅支持“按 run 批量执行”，按题执行请先清空上方 run 多选</Typography.Text>
                    ) : null}
                    {(activeJudgeTask?.status || judgeJob?.status) ? (
                      <Typography.Text type={(activeJudgeTaskRunning || persistedJudgeRunning) ? 'warning' : (String(activeJudgeTask?.status || judgeJob?.status) === 'failed' ? 'danger' : 'secondary')}>
                        {activeJudgeTaskRunning || persistedJudgeRunning
                          ? `Judge 运行中 ${judgeCompleted}/${judgeTotal || 0}`
                          : String(activeJudgeTask?.status || judgeJob?.status) === 'completed'
                            ? `Judge 已完成 ${judgeCompleted}/${judgeTotal || 0}`
                            : String(activeJudgeTask?.status || judgeJob?.status) === 'failed'
                              ? `Judge 失败 ${(activeJudgeTask?.errors || [])[0] || judgeJob.last_error || ''}`
                              : `Judge 状态: ${String(activeJudgeTask?.status || judgeJob?.status)}`}
                      </Typography.Text>
                    ) : null}
                    {((activeJudgeTaskRunning || persistedJudgeRunning) && judgeTotal > 0) ? (
                      <Progress
                        percent={judgePercent}
                        size="small"
                        style={{ width: 180 }}
                        status="active"
                        format={() => `${judgeCompleted}/${judgeTotal}`}
                      />
                    ) : null}
                    {judgeBatchProgress.running ? (
                      <Space size={6}>
                        <Typography.Text type="warning">
                          批量Judge进行中 {judgeBatchProgress.completed}/{judgeBatchProgress.total}
                        </Typography.Text>
                        <Progress
                          percent={batchJudgePercent}
                          size="small"
                          style={{ width: 220 }}
                          status="active"
                          format={() => `${judgeBatchProgress.completed}/${judgeBatchProgress.total}`}
                        />
                        {judgeBatchProgress.currentRunId ? (
                          <Typography.Text type="secondary">当前: {judgeBatchProgress.currentRunId}</Typography.Text>
                        ) : null}
                      </Space>
                    ) : null}
                    <Typography.Text type="secondary">已选 {judgeSelectedQuestionIds.length} 题</Typography.Text>
                    <Typography.Text type="secondary">
                      {judgeSelectionIsSavedOnly ? '当前仅展示并允许选择落库题' : '当前仅展示并允许选择落库题'}
                    </Typography.Text>
                  </Space>
                )}
                <Table
                size="small"
                loading={loading || judgeBatchLoading}
                rowKey={(r) => r.__row_key || r.question_id || `${r.index}`}
                dataSource={judgeTableRows}
                pagination={{ pageSize: 20 }}
                tableLayout="fixed"
                scroll={{ x: 2600 }}
                rowSelection={isBatchQuestionView ? undefined : {
                  selectedRowKeys: judgeSelectedQuestionIds,
                  onChange: (_, selectedRows) => setJudgeSelectedQuestionIds(selectedRows.map((r) => r.question_id).filter(Boolean)),
                  getCheckboxProps: (record) => ({ disabled: !record.question_id }),
                }}
                columns={[
                  ...(isBatchQuestionView ? [{
                    title: 'run',
                    width: 260,
                    fixed: 'left',
                    ellipsis: true,
                    render: (_, r) => (
                      <Tooltip title={r.__run_task_name ? `${r.__run_task_name} | ${r.__run_id}` : (r.__run_id || '-')}>
                        <span>{r.__run_id || '-'}</span>
                      </Tooltip>
                    ),
                  }] : []),
                  { title: '题号', dataIndex: 'index', width: 72, fixed: 'left' },
                  { title: 'question_id', dataIndex: 'question_id', width: 240, ellipsis: true, fixed: 'left' },
                  {
                    title: '题目信息',
                    key: 'question_text',
                    width: 260,
                    ellipsis: true,
                    fixed: 'left',
                    render: (_, r) => (r.question_text ? (
                      <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{r.question_text}</span>}>
                        <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.question_text}</span>
                      </Tooltip>
                    ) : '—'),
                  },
                  { title: 'critic通过', width: 100, render: (_, r) => (r.hard_gate?.pass ? <Tag color="green">PASS</Tag> : <Tag color="red">FAIL</Tag>) },
                  { title: 'Judge', render: (_, r) => (() => { const oj = r.offline_judge; if (oj?.error) return <Tooltip title={oj.error}><Tag color="red">失败</Tag></Tooltip>; const d = (oj?.decision || '').toLowerCase(); if (!d) return '—'; return <Tag color={d === 'pass' ? 'green' : d === 'reject' ? 'red' : 'orange'}>{d.toUpperCase()}</Tag>; })(), width: 88 },
                  { title: '融合总分', render: (_, r) => toTenPointScore(r.offline_judge?.overall_score) != null ? Number(toTenPointScore(r.offline_judge?.overall_score)).toFixed(2) : '—', width: 96 },
                  { title: '基线分', render: (_, r) => toTenPointScore(getJudgeBaselineScore(r.offline_judge)) != null ? Number(toTenPointScore(getJudgeBaselineScore(r.offline_judge))).toFixed(2) : '—', width: 88 },
                  { title: '质量分', render: (_, r) => toTenPointScore(r.offline_judge?.quality_score) != null ? Number(toTenPointScore(r.offline_judge?.quality_score)).toFixed(2) : '—', width: 88 },
                  {
                    title: 'Judge reject 原因',
                    width: 200,
                    ellipsis: true,
                    render: (_, r) => {
                      const oj = r.offline_judge;
                      if (!oj) return '—';
                      const reasons = (oj.reasons || []).filter(Boolean);
                      const feedback = (oj.actionable_feedback || '').trim();
                      const feedbackOnly = feedback && !reasons.includes(feedback) ? feedback : '';
                      const summary = reasons.length ? reasons.join('；') : (feedbackOnly || '—');
                      if (!summary || summary === '—') return '—';
                      const full = feedbackOnly ? `${reasons.join('；')}\n可执行建议: ${feedbackOnly}` : reasons.join('；');
                      return (
                        <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{full}</span>}>
                          <span style={{ cursor: 'help' }}>{summary.length > 36 ? `${summary.slice(0, 36)}…` : summary}</span>
                        </Tooltip>
                      );
                    },
                  },
                  {
                    title: 'Judge维度（状态与分数）',
                    width: 280,
                    ellipsis: true,
                    render: (_, r) => {
                      const rows = judgeDimensionDisplayRows(r.offline_judge);
                      if (!rows.length) return '—';
                      return (
                        <Space size={[4, 4]} wrap>
                          {rows.map(({ name, status, score }) => {
                            const color = status === 'PASS' ? 'green' : status === 'FAIL' ? 'red' : 'default';
                            const scoreStr = score != null ? ` ${score}分` : '';
                            return <Tag key={name} color={color}>{name}:{status}{scoreStr}</Tag>;
                          })}
                        </Space>
                      );
                    },
                  },
                  { title: 'critic loops', render: (_, r) => r.stability?.critic_loops ?? 0, width: 100 },
                  { title: '出题LLM calls', render: (_, r) => r.stability?.llm_calls ?? 0, width: 110 },
                  { title: '出题tokens', render: (_, r) => r.stability?.tokens ?? 0, width: 100 },
                  { title: '出题latency(ms)', render: (_, r) => r.stability?.latency_ms ?? 0, width: 130 },
                  { title: 'Judge calls', render: (_, r) => r.offline_judge?.observability?.llm_calls ?? 0, width: 100 },
                  { title: 'Judge tokens', render: (_, r) => r.offline_judge?.observability?.tokens?.total_tokens ?? 0, width: 110 },
                  { title: 'Judge latency(ms)', render: (_, r) => r.offline_judge?.observability?.latency_ms ?? 0, width: 130 },
                ]}
                expandable={{
                  expandedRowRender: (r) => (
                    <>
{r.offline_judge ? (
                          <Card size="small" title="离线 Judge 报告" style={{ marginBottom: 12 }}>
                            {r.offline_judge.error ? (
                              <Typography.Text type="danger">Judge 失败: {r.offline_judge.error}</Typography.Text>
                            ) : (
                              <Descriptions size="small" column={1} bordered>
                                <Descriptions.Item label="结论">
                                  <Tag color={(r.offline_judge.decision || '').toLowerCase() === 'reject' ? 'red' : (r.offline_judge.decision || '').toLowerCase() === 'pass' ? 'green' : 'orange'}>
                                    {(r.offline_judge.decision || '').toUpperCase()}
                                  </Tag>
                                  {toTenPointScore(r.offline_judge.overall_score) != null && ` · 融合总分 ${Number(toTenPointScore(r.offline_judge.overall_score)).toFixed(2)}`}
                                  {toTenPointScore(getJudgeBaselineScore(r.offline_judge)) != null && ` · 基线分 ${Number(toTenPointScore(getJudgeBaselineScore(r.offline_judge))).toFixed(2)}`}
                                  {toTenPointScore(r.offline_judge.quality_score) != null && ` · 质量分 ${Number(toTenPointScore(r.offline_judge.quality_score)).toFixed(2)}`}
                                </Descriptions.Item>
                                <Descriptions.Item label="维度分数">
                                  <Space size={[4, 4]} wrap>
                                    {judgeDimensionDisplayRows(r.offline_judge).map(({ name, status, score }) => (
                                      <Tag
                                        key={`${name}_${status}`}
                                        color={status === 'PASS' ? 'green' : status === 'FAIL' ? 'red' : 'default'}
                                      >
                                        {name}:{status}{score != null ? ` ${score}分` : ''}
                                      </Tag>
                                    ))}
                                  </Space>
                                </Descriptions.Item>
                                <Descriptions.Item label="Reject/Review 原因">
                                  {(r.offline_judge.reasons || []).join(' | ') || '-'}
                                </Descriptions.Item>
                                <Descriptions.Item label="可执行建议">
                                  {r.offline_judge.actionable_feedback && !(r.offline_judge.reasons || []).includes(r.offline_judge.actionable_feedback)
                                    ? r.offline_judge.actionable_feedback
                                    : '-'}
                                </Descriptions.Item>
                                {r.offline_judge.hard_gate?.details?.short_circuit_evidence_chain &&
                                r.offline_judge.hard_gate.details.short_circuit_evidence_chain.length ? (
                                  <Descriptions.Item label="知识门短路证据链">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {r.offline_judge.hard_gate.details.short_circuit_evidence_chain.join('\n')}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.semantic_drift?.details?.short_circuit_evidence_chain &&
                                r.offline_judge.semantic_drift.details.short_circuit_evidence_chain.length ? (
                                  <Descriptions.Item label="语义漂移短路证据链">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {r.offline_judge.semantic_drift.details.short_circuit_evidence_chain.join('\n')}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.solver_validation ? (
                                  <Descriptions.Item label="Solver 校验">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {[
                                        r.offline_judge.solver_validation.verdict ? `verdict=${r.offline_judge.solver_validation.verdict}` : '',
                                        r.offline_judge.solver_validation.predicted_answer ? `predicted=${r.offline_judge.solver_validation.predicted_answer}` : '',
                                        r.offline_judge.solver_validation.reasoning_path ? `reasoning=${r.offline_judge.solver_validation.reasoning_path}` : '',
                                      ].filter(Boolean).join('\n') || '-'}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.observability ? (
                                  <Descriptions.Item label="Judge 资源（独立）">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {JSON.stringify(r.offline_judge.observability, null, 2)}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.costs ? (
                                  <Descriptions.Item label="Judge 成本（USD）">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {JSON.stringify(r.offline_judge.costs, null, 2)}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.risk_assessment ? (
                                  <Descriptions.Item label="风险评估">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {JSON.stringify(r.offline_judge.risk_assessment, null, 2)}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.scores ? (
                                  <Descriptions.Item label="Judge 分项得分">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                      {JSON.stringify(r.offline_judge.scores, null, 2)}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                                {r.offline_judge.debug_solver_raw_response_preview ? (
                                  <Descriptions.Item label="Solver 原始返回预览">
                                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} copyable>
                                      {r.offline_judge.debug_solver_raw_response_preview}
                                    </Typography.Paragraph>
                                  </Descriptions.Item>
                                ) : null}
                              </Descriptions>
                            )}
                          </Card>
                        ) : null}
                      <Descriptions size="small" column={1} bordered>
                        <Descriptions.Item label="judge_status">
                          {judgeJob?.status
                            ? `${judgeJob.status} | ${Number(judgeJob.completed_count || 0)}/${Number(judgeJob.requested_count || 0)}`
                            : '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="run_id">
                          {r.__run_id || selectedRunId || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="question_id">
                          {r.question_id || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="题干">
                          {r.judge_input?.stem || r.question_text || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="选项">
                          {(r.judge_input?.options || [])
                            .map((opt, idx) => `${String.fromCharCode(65 + idx)}. ${opt}`)
                            .join('  ') || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="正确答案">
                          {r.judge_input?.correct_answer || r.answer || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="解析">
                          {r.judge_input?.explanation || r.issues?.reason || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="切片原文">
                          {r.judge_input?.textbook_slice || r.slice_content || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="failed_rules">
                          {(r.hard_gate?.failed_rules || []).join(' | ') || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="quality_issues">
                          {(r.issues?.quality_issues || []).join(' | ') || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="missing_conditions">
                          {(r.issues?.missing_conditions || []).join(' | ') || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="all_issues">
                          {(r.issues?.all_issues || []).join(' | ') || '-'}
                        </Descriptions.Item>
                        <Descriptions.Item label="fix_strategy">
                          {r.issues?.fix_strategy || '-'}
                        </Descriptions.Item>
                      </Descriptions>
                    </>
                  ),
                }}
              />
              </Space>
            ),
          },
          {
            key: 'llm_calls',
            label: '出题链路模型调用明细',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Card size="small">
                  <Space wrap>
                    <Select
                      style={{ width: 420 }}
                      value={llmQuestionFilter || undefined}
                      allowClear
                      showSearch
                      placeholder="按 question_id 过滤（可选）"
                      onChange={(v) => setLlmQuestionFilter(v || '')}
                      options={questionRows.map((q) => ({ label: q.question_id, value: q.question_id }))}
                    />
                    <Button onClick={() => onRefreshCurrentRun(selectedRunId)} disabled={!selectedRunId}>应用筛选</Button>
                    <Button onClick={() => { setLlmQuestionFilter(''); onRefreshCurrentRun(selectedRunId); }} disabled={!selectedRunId}>清空筛选</Button>
                  </Space>
                </Card>
                <Card size="small" title="按节点聚合">
                  <Table
                    size="small"
                    rowKey={(r) => `${r.node}|${r.model}`}
                    dataSource={callAggByNode}
                    pagination={false}
                    columns={[
                      { title: 'node', dataIndex: 'node' },
                      { title: 'model', dataIndex: 'model' },
                      { title: 'calls', dataIndex: 'calls', width: 90 },
                      { title: 'tokens', dataIndex: 'tokens', width: 100 },
                      { title: 'latency(ms)', dataIndex: 'latency_ms', width: 120, render: (v) => Number(v || 0).toFixed(2) },
                      { title: 'errors', dataIndex: 'errors', width: 90 },
                    ]}
                  />
                </Card>
                <Table
                  size="small"
                  loading={loading}
                  rowKey={(r, i) => `${r.trace_id || ''}_${r.ts || ''}_${i}`}
                  dataSource={llmCalls}
                  pagination={{ pageSize: 30 }}
                  columns={[
                    { title: 'question_id', dataIndex: 'question_id', width: 180, ellipsis: true },
                    { title: 'node', dataIndex: 'node', width: 150 },
                    { title: 'model', dataIndex: 'model', width: 150, ellipsis: true },
                    { title: 'provider', dataIndex: 'provider', width: 130 },
                    { title: 'success', dataIndex: 'success', width: 80, render: (v) => (v ? <Tag color="green">true</Tag> : <Tag color="red">false</Tag>) },
                    { title: 'error', dataIndex: 'error', ellipsis: true },
                    { title: 'retries', dataIndex: 'retries', width: 80 },
                    { title: 'prompt', dataIndex: 'prompt_tokens', width: 90 },
                    { title: 'completion', dataIndex: 'completion_tokens', width: 110 },
                    { title: 'total', dataIndex: 'total_tokens', width: 90 },
                    { title: 'latency(ms)', dataIndex: 'latency_ms', width: 110 },
                    { title: 'ts', dataIndex: 'ts', width: 170 },
                  ]}
                  scroll={{ x: 1900 }}
                />
              </Space>
            ),
          },
          {
            key: 'batch',
            label: '批量指标',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Card loading={loading} title="Critic 不通过类型统计">
                  <Table
                    size="small"
                    rowKey={(r) => r.type}
                    pagination={false}
                    dataSource={Object.entries(runDetail?.batch_metrics?.critic_fail_type_counts || {}).map(([type, count]) => ({
                      type,
                      label: CRITIC_FAIL_TYPE_LABELS[type] || type,
                      count,
                    })).sort((a, b) => b.count - a.count)}
                    columns={[
                      { title: '类型', dataIndex: 'label', key: 'label' },
                      { title: '次数', dataIndex: 'count', key: 'count', width: 100 },
                    ]}
                  />
                  {!Object.keys(runDetail?.batch_metrics?.critic_fail_type_counts || {}).length && (
                    <Text type="secondary">本 run 无 Critic 不通过题目或暂无统计</Text>
                  )}
                </Card>
                <Card loading={loading}>
                  <Descriptions size="small" bordered column={2}>
                    {Object.entries(runDetail?.batch_metrics || {}).filter(([k]) => k !== 'critic_fail_type_counts').map(([k, v]) => (
                      <Descriptions.Item key={k} label={k}>
                        {v == null ? '—' : typeof v === 'number' ? Number(v).toFixed(4) : String(v)}
                      </Descriptions.Item>
                    ))}
                  </Descriptions>
                </Card>
              </Space>
            ),
          },
          {
            key: 'trend',
            label: '趋势',
            children: (
              <Table
                size="small"
                loading={loading}
                rowKey={(r) => `${r.run_id}_${r.date}`}
                dataSource={trends}
                pagination={{ pageSize: 20 }}
                columns={[
                  { title: 'date', dataIndex: 'date', width: 120 },
                  { title: 'run_id', dataIndex: 'run_id', ellipsis: true },
                  { title: '入库率', dataIndex: 'hard_pass_rate' },
                  { title: 'quality_score_avg', dataIndex: 'quality_score_avg' },
                  { title: 'risk_high_rate', dataIndex: 'risk_high_rate' },
                  { title: 'logic_pass_rate', dataIndex: 'logic_pass_rate' },
                  { title: 'avg_tokens_per_question', dataIndex: 'avg_tokens_per_question' },
                  { title: 'avg_latency_ms_per_question', dataIndex: 'avg_latency_ms_per_question' },
                  { title: 'avg_cost_per_question', dataIndex: 'avg_cost_per_question' },
                  { title: 'cpvq', dataIndex: 'cpvq', render: (v) => v != null ? Number(v).toFixed(4) : '—' },
                  { title: 'total_cost', dataIndex: 'total_cost' },
                  { title: 'error_call_rate', dataIndex: 'error_call_rate' },
                ]}
              />
            ),
          },
          {
            key: 'drift',
            label: '漂移对比',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Space wrap align="center">
                  <Select style={{ width: 360 }} placeholder="基线 run（当前上线版本）" value={driftBase || undefined} onChange={setDriftBase} options={runs.map((r) => { const name = (r.task_name || '').trim() || r.task_id || r.run_id; const date = (r.ended_at || '').slice(0, 10); return { label: date ? `${name} | ${date}` : name, value: r.run_id }; })} />
                  <Select style={{ width: 360 }} placeholder="目标 run（待评估版本）" value={driftTarget || undefined} onChange={setDriftTarget} options={runs.map((r) => { const name = (r.task_name || '').trim() || r.task_id || r.run_id; const date = (r.ended_at || '').slice(0, 10); return { label: date ? `${name} | ${date}` : name, value: r.run_id }; })} />
                  <Button onClick={onRunDrift}>执行对比</Button>
                  <Button type="default" onClick={onSetBaseline}>将当前基线 run 设为发布基线</Button>
                </Space>
                <Text type="secondary">基线优先使用「<Link to="/version-management">版本管理</Link>」中最新已发布版本；未发布时可用「设为发布基线」暂存当前所选 run。</Text>
                <Table
                  size="small"
                  rowKey={(r) => r.metric}
                  dataSource={Object.entries(driftResult?.compare || {}).map(([k, v]) => ({ metric: k, ...(v || {}) }))}
                  pagination={false}
                  columns={[
                    { title: 'metric', dataIndex: 'metric' },
                    { title: 'base', dataIndex: 'base' },
                    { title: 'target', dataIndex: 'target' },
                    { title: 'delta', dataIndex: 'delta', render: (v) => <Text type={Number(v || 0) >= 0 ? 'danger' : 'success'}>{v}</Text> },
                  ]}
                />
              </Space>
            ),
          },
          {
            key: 'release_report',
            label: '发布评估',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Space wrap align="center">
                  <Select style={{ width: 360 }} placeholder="基线 run（当前上线版本）" value={driftBase || undefined} onChange={setDriftBase} options={runs.map((r) => { const name = (r.task_name || '').trim() || r.task_id || r.run_id; const date = (r.ended_at || '').slice(0, 10); return { label: date ? `${name} | ${date}` : name, value: r.run_id }; })} />
                  <Select style={{ width: 360 }} placeholder="目标 run（待评估版本）" value={driftTarget || undefined} onChange={setDriftTarget} options={runs.map((r) => { const name = (r.task_name || '').trim() || r.task_id || r.run_id; const date = (r.ended_at || '').slice(0, 10); return { label: date ? `${name} | ${date}` : name, value: r.run_id }; })} />
                  <Button onClick={onRunReleaseReport}>生成发布结论</Button>
                  <Button type="default" onClick={onSetBaseline}>将当前基线 run 设为发布基线</Button>
                </Space>
                <Text type="secondary">基线优先使用「<Link to="/version-management">版本管理</Link>」最新发布版本。</Text>
                {releaseReport ? (
                  <Card size="small">
                    <Descriptions size="small" bordered column={2}>
                      <Descriptions.Item label="verdict">
                        <Tag color={releaseReport.verdict === 'promote' ? 'green' : releaseReport.verdict === 'rollback' ? 'red' : 'orange'}>
                          {releaseReport.verdict}
                        </Tag>
                      </Descriptions.Item>
                      <Descriptions.Item label="conclusion">{releaseReport.conclusion}</Descriptions.Item>
                      <Descriptions.Item label="win_count">{releaseReport.win_count}</Descriptions.Item>
                      <Descriptions.Item label="lose_count">{releaseReport.lose_count}</Descriptions.Item>
                    </Descriptions>
                    <Table
                      size="small"
                      rowKey={(r) => r.metric}
                      dataSource={releaseReport.rows || []}
                      pagination={false}
                      columns={[
                        { title: 'metric', dataIndex: 'metric' },
                        { title: 'base', dataIndex: 'base' },
                        { title: 'target', dataIndex: 'target' },
                        { title: 'delta', dataIndex: 'delta' },
                        { title: 'decision', dataIndex: 'decision', render: (v) => <Tag color={v === 'better' ? 'green' : v === 'worse' ? 'red' : 'blue'}>{v}</Tag> },
                      ]}
                    />
                  </Card>
                ) : null}
              </Space>
            ),
          },
          {
            key: 'ops_weekly',
            label: '运营周报',
            children: (
              <Card size="small">
                <Descriptions size="small" bordered column={2}>
                  <Descriptions.Item label="window_start">{opsWeekly.window_start || '-'}</Descriptions.Item>
                  <Descriptions.Item label="window_end">{opsWeekly.window_end || '-'}</Descriptions.Item>
                  <Descriptions.Item label="total_alerts">{opsWeekly.total_alerts || 0}</Descriptions.Item>
                  <Descriptions.Item label="open_alerts">{opsWeekly.open_alerts || 0}</Descriptions.Item>
                  <Descriptions.Item label="resolved_alerts">{opsWeekly.resolved_alerts || 0}</Descriptions.Item>
                  <Descriptions.Item label="overdue_alerts">{opsWeekly.overdue_alerts || 0}</Descriptions.Item>
                  <Descriptions.Item label="high_alerts">{opsWeekly.high_alerts || 0}</Descriptions.Item>
                  <Descriptions.Item label="mttr_hours">{Number(opsWeekly.mttr_hours || 0).toFixed(3)}</Descriptions.Item>
                  <Descriptions.Item label="resolution_rate">{pct(opsWeekly.resolution_rate || 0)}</Descriptions.Item>
                </Descriptions>
                <Table
                  size="small"
                  style={{ marginTop: 12 }}
                  rowKey={(r) => r.owner}
                  dataSource={opsWeekly.owner_breakdown || []}
                  pagination={false}
                  columns={[
                    { title: 'owner', dataIndex: 'owner' },
                    { title: 'count', dataIndex: 'count', width: 120 },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'alerts',
            label: '风险告警',
            children: (
              <Table
                size="small"
                loading={loading}
                rowKey={(r) => r.alert_id}
                dataSource={alerts}
                pagination={{ pageSize: 20 }}
                columns={[
                  { title: 'alert_id', dataIndex: 'alert_id', width: 260, ellipsis: true },
                  { title: 'level', dataIndex: 'level', width: 90, render: (v) => <Tag color={v === 'high' ? 'red' : v === 'medium' ? 'orange' : 'blue'}>{v}</Tag> },
                  { title: 'type', dataIndex: 'type', width: 120 },
                  { title: 'message', dataIndex: 'message', ellipsis: true },
                  { title: 'run_id', dataIndex: 'run_id', width: 220, ellipsis: true },
                  { title: 'question_id', dataIndex: 'question_id', width: 220, ellipsis: true },
                  { title: 'owner', dataIndex: 'owner', width: 120 },
                  { title: 'sla_due_at', dataIndex: 'sla_due_at', width: 180 },
                  { title: 'overdue', dataIndex: 'overdue', width: 90, render: (v) => (v ? <Tag color="red">true</Tag> : <Tag color="green">false</Tag>) },
                  { title: 'status', dataIndex: 'status', width: 100 },
                  {
                    title: '操作',
                    width: 220,
                    render: (_, r) => (
                      <Space>
                        <Button size="small" onClick={() => onAlertStatus(r.alert_id, 'ack')}>确认</Button>
                        <Button size="small" onClick={() => onAlertStatus(r.alert_id, 'resolved')}>解决</Button>
                        <Button size="small" onClick={() => onAlertStatus(r.alert_id, 'ignored')}>忽略</Button>
                      </Space>
                    ),
                  },
                ]}
                scroll={{ x: 1600 }}
              />
            ),
          },
          {
            key: 'threshold',
            label: '阈值配置',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Table
                  size="small"
                  pagination={false}
                  rowKey={(r) => r.key}
                  dataSource={Object.entries(thresholds || {}).map(([k, v]) => ({ key: k, value: Number(v || 0) }))}
                  columns={[
                    { title: '阈值键', dataIndex: 'key', width: 320 },
                    {
                      title: '阈值',
                      dataIndex: 'value',
                      render: (_, r) => (
                        <InputNumber
                          value={Number(thresholds?.[r.key] || 0)}
                          step={0.01}
                          onChange={(v) => setThresholds((prev) => ({ ...(prev || {}), [r.key]: Number(v || 0) }))}
                        />
                      ),
                    },
                  ]}
                />
                <Button type="primary" onClick={onSaveThresholds}>保存阈值</Button>
              </Space>
            ),
          },
        ]}
      />
    </Space>
  );
}
