import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Modal,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { useNavigate, useParams } from 'react-router-dom';
import { cancelGenerateTask, getGenerateTask, getSlices, getSliceImageUrl, updateGenerateTaskBankPolicy } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import MarkdownWithMermaid from '../components/MarkdownWithMermaid';
import QuestionDetailView from '../components/QuestionDetailView';
import { getFinalQuestionPreviewCardMeta } from '../utils/finalQuestionPreviewMeta';

function displayCurrencyUnit(currency) {
  const c = String(currency || 'CNY').trim().toUpperCase();
  if (c === 'CNY' || c === 'RMB' || c === 'CNH') return '元';
  return c || '元';
}

function formatAmount(value, currency, digits = 4) {
  return `${Number(value || 0).toFixed(digits)} ${displayCurrencyUnit(currency)}`;
}

export default function AIGenerateTaskDetailPage() {
  const navigate = useNavigate();
  const { taskId } = useParams();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [task, setTask] = useState({});
  const [approvedSlices, setApprovedSlices] = useState([]);
  const [viewQuestionOpen, setViewQuestionOpen] = useState(false);
  const [viewQuestionRecord, setViewQuestionRecord] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  const [updatingBankPolicy, setUpdatingBankPolicy] = useState(false);
  const [nowMs, setNowMs] = useState(Date.now());
  const slicesLoadedMaterialRef = useRef('');

  const stableStringify = (value) => {
    const seen = new WeakSet();
    const normalize = (input) => {
      if (Array.isArray(input)) return input.map((item) => normalize(item));
      if (input && typeof input === 'object') {
        if (seen.has(input)) return '[Circular]';
        seen.add(input);
        const out = {};
        Object.keys(input).sort().forEach((key) => {
          out[key] = normalize(input[key]);
        });
        return out;
      }
      return input;
    };
    try {
      return JSON.stringify(normalize(value));
    } catch (_) {
      return String(value ?? '');
    }
  };

  const traceSignature = (trace) => {
    const list = Array.isArray(trace) ? trace : [];
    return stableStringify(list.map((item) => ({
      index: Number(item?.index || 0),
      target_index: Number(item?.target_index || 0),
      elapsed_ms: Number(item?.elapsed_ms || 0),
      saved: Boolean(item?.saved),
      saved_with_issues: Boolean(item?.saved_with_issues),
      snapshot_stage: String(item?.snapshot_stage || ''),
      question_id: String(item?.question_id || ''),
      trace_id: String(item?.trace_id || ''),
      question_type: String(item?.question_type || ''),
      slice_id: Number(item?.slice_id || 0),
      final_json_expired: Boolean(item?.final_json_expired),
      final_json_expired_at: String(item?.final_json_expired_at || ''),
      final_json_run_id: Number(item?.final_json_run_id || 0),
      critic_result: item?.critic_result || {},
      critic_details: String(item?.critic_details || ''),
      critic_last_fail_types: item?.critic_last_fail_types || [],
      critic_last_error_content: String(item?.critic_last_error_content || ''),
      final_json: item?.final_json || {},
      steps: Array.isArray(item?.steps) ? item.steps.map((step) => ({
        seq: Number(step?.seq || 0),
        node: String(step?.node || ''),
        level: String(step?.level || ''),
        message: String(step?.message || ''),
        detail: String(step?.detail || ''),
        run_id: Number(step?.run_id || 0),
      })) : [],
    })));
  };

  const itemsSignature = (items) => {
    const list = Array.isArray(items) ? items : [];
    return stableStringify(list);
  };

  const errorsSignature = (errs) => {
    const list = Array.isArray(errs) ? errs : [];
    return stableStringify(list);
  };

  const listSignature = (value) => {
    const list = Array.isArray(value) ? value : [];
    return stableStringify(list);
  };

  /**
   * 统计逐题过程里“成功产出”的题目数（按目标题位去重后的一条记录）。
   * 成功定义：saved=true 或 saved_with_issues=true，且存在可用 final_json。
   * @param {Array<Record<string, any>>} traceRows
   * @returns {number}
   */
  const countTraceSuccess = (traceRows) => {
    const rows = Array.isArray(traceRows) ? traceRows : [];
    return rows.filter((row) => {
      if (!row || typeof row !== 'object') return false;
      const saved = Boolean(row.saved) || Boolean(row.saved_with_issues);
      if (!saved) return false;
      const fj = row.final_json;
      return Boolean(fj && typeof fj === 'object' && !Array.isArray(fj) && Object.keys(fj).length > 0);
    }).length;
  };

  const mergeTaskForRender = (prevTask, nextTask) => {
    const prev = prevTask && typeof prevTask === 'object' ? prevTask : {};
    const next = nextTask && typeof nextTask === 'object' ? nextTask : {};
    const merged = { ...prev, ...next };

    const prevTrace = Array.isArray(prev.process_trace) ? prev.process_trace : [];
    const nextTrace = Array.isArray(next.process_trace) ? next.process_trace : [];
    merged.process_trace = traceSignature(prevTrace) === traceSignature(nextTrace) ? prevTrace : nextTrace;

    const prevItems = Array.isArray(prev.items) ? prev.items : [];
    const nextItems = Array.isArray(next.items) ? next.items : [];
    merged.items = itemsSignature(prevItems) === itemsSignature(nextItems) ? prevItems : nextItems;

    const prevErrors = Array.isArray(prev.errors) ? prev.errors : [];
    const nextErrors = Array.isArray(next.errors) ? next.errors : [];
    merged.errors = errorsSignature(prevErrors) === errorsSignature(nextErrors) ? prevErrors : nextErrors;

    const prevSubtasks = Array.isArray(prev.subtasks) ? prev.subtasks : [];
    const nextSubtasks = Array.isArray(next.subtasks) ? next.subtasks : [];
    merged.subtasks = listSignature(prevSubtasks) === listSignature(nextSubtasks) ? prevSubtasks : nextSubtasks;

    const prevRepairRounds = Array.isArray(prev.repair_rounds) ? prev.repair_rounds : [];
    const nextRepairRounds = Array.isArray(next.repair_rounds) ? next.repair_rounds : [];
    merged.repair_rounds = listSignature(prevRepairRounds) === listSignature(nextRepairRounds) ? prevRepairRounds : nextRepairRounds;

    const prevSliceFailureStats = Array.isArray(prev.slice_failure_stats) ? prev.slice_failure_stats : [];
    const nextSliceFailureStats = Array.isArray(next.slice_failure_stats) ? next.slice_failure_stats : [];
    merged.slice_failure_stats = listSignature(prevSliceFailureStats) === listSignature(nextSliceFailureStats)
      ? prevSliceFailureStats
      : nextSliceFailureStats;

    const prevLiveSubtaskTraces = Array.isArray(prev.live_subtask_traces) ? prev.live_subtask_traces : [];
    const nextLiveSubtaskTraces = Array.isArray(next.live_subtask_traces) ? next.live_subtask_traces : [];
    merged.live_subtask_traces = listSignature(prevLiveSubtaskTraces) === listSignature(nextLiveSubtaskTraces)
      ? prevLiveSubtaskTraces
      : nextLiveSubtaskTraces;

    const prevCurrentSubcall = prev.current_subcall && typeof prev.current_subcall === 'object' ? prev.current_subcall : {};
    const nextCurrentSubcall = next.current_subcall && typeof next.current_subcall === 'object' ? next.current_subcall : {};
    merged.current_subcall = stableStringify(prevCurrentSubcall) === stableStringify(nextCurrentSubcall)
      ? prevCurrentSubcall
      : nextCurrentSubcall;

    const prevProgress = prev.progress && typeof prev.progress === 'object' ? prev.progress : {};
    const nextProgress = next.progress && typeof next.progress === 'object' ? next.progress : {};
    const sameProgress = Number(prevProgress.current || 0) === Number(nextProgress.current || 0)
      && Number(prevProgress.total || 0) === Number(nextProgress.total || 0);
    merged.progress = sameProgress ? prevProgress : nextProgress;

    const changed = (
      prev !== merged
      && (
        String(prev.status || '') !== String(merged.status || '')
        || String(prev.current_node || '') !== String(merged.current_node || '')
        || String(prev.current_node_updated_at || '') !== String(merged.current_node_updated_at || '')
        || Number(prev.generated_count || 0) !== Number(merged.generated_count || 0)
        || Number(prev.saved_count || 0) !== Number(merged.saved_count || 0)
        || Number(prev.error_count || 0) !== Number(merged.error_count || 0)
        || prev.process_trace !== merged.process_trace
        || prev.items !== merged.items
        || prev.errors !== merged.errors
        || prev.subtasks !== merged.subtasks
        || prev.repair_rounds !== merged.repair_rounds
        || prev.slice_failure_stats !== merged.slice_failure_stats
        || prev.live_subtask_traces !== merged.live_subtask_traces
        || prev.current_subcall !== merged.current_subcall
        || prev.progress !== merged.progress
      )
    );
    return changed ? merged : prev;
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadDetail = async ({ silent = false, forceSliceReload = false } = {}) => {
    if (!tenantId || !taskId) return;
    const isLegacyTaskId = String(taskId || '').startsWith('legacy_');
    if (isLegacyTaskId) {
      if (!silent) {
        message.warning('该历史任务不支持详情页查看，已返回任务列表');
      }
      navigate('/ai-generate', { replace: true });
      return;
    }
    if (!silent) setLoading(true);
    try {
      const res = await getGenerateTask(tenantId, taskId);
      setTask((prev) => mergeTaskForRender(prev, res?.task || {}));
      const materialVersionId = String(res?.task?.material_version_id || res?.task?.request?.material_version_id || '').trim();
      if (materialVersionId) {
        const shouldReloadSlices = forceSliceReload || slicesLoadedMaterialRef.current !== materialVersionId;
        if (shouldReloadSlices) {
          try {
            const sliceRes = await getSlices(tenantId, {
              status: 'approved',
              material_version_id: materialVersionId,
              page: 1,
              page_size: 200,
            });
            setApprovedSlices(Array.isArray(sliceRes?.items) ? sliceRes.items : []);
            slicesLoadedMaterialRef.current = materialVersionId;
          } catch (_e) {
            setApprovedSlices([]);
          }
        }
      } else {
        setApprovedSlices([]);
        slicesLoadedMaterialRef.current = '';
      }
    } catch (e) {
      if (!silent) {
        message.error(e?.response?.data?.error?.message || '加载任务详情失败');
      }
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    loadDetail({ forceSliceReload: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, taskId]);

  const items = useMemo(() => (Array.isArray(task?.items) ? task.items : []), [task]);
  const processTrace = useMemo(() => (Array.isArray(task?.process_trace) ? task.process_trace : []), [task]);
  const isTaskActive = ['pending', 'running'].includes(String(task?.status || ''));
  // Normalize legacy task-timeout errors (task-level timeout has been removed)
  const errors = useMemo(() => {
    const raw = Array.isArray(task?.errors) ? task.errors : [];
    return raw.map((e) => {
      const s = String(e || '').trim();
      if (/任务执行超时|task\s*execution\s*timeout/i.test(s)) {
        return '任务执行失败（当前版本已取消任务执行时间限制；该错误可能来自历史任务记录）';
      }
      return s;
    });
  }, [task?.errors]);

  const approvedSliceById = useMemo(() => {
    const m = new Map();
    (approvedSlices || []).forEach((row) => {
      const sid = Number(row?.slice_id || 0);
      if (!sid) return;
      m.set(sid, row);
    });
    return m;
  }, [approvedSlices]);

  const formatSliceContent = (txt) => String(txt || '');
  const subtasks = useMemo(() => (Array.isArray(task?.subtasks) ? task.subtasks : []), [task]);
  const repairRounds = useMemo(() => (Array.isArray(task?.repair_rounds) ? task.repair_rounds : []), [task]);
  const sliceFailureStats = useMemo(() => (Array.isArray(task?.slice_failure_stats) ? task.slice_failure_stats : []), [task]);
  const currentSubcall = useMemo(() => (
    task?.current_subcall && typeof task.current_subcall === 'object' ? task.current_subcall : {}
  ), [task]);
  const subtaskCount = Number(task?.subtask_count || 0);
  const repairRoundCount = Number(task?.repair_round_count || 0);
  const failureSliceCount = Number(task?.failure_slice_count || 0);

  const NODE_LABELS = { router: '路由', specialist: '初稿', writer: '作家', critic: '审核', fixer: '修复', calculator: '计算', system: '系统' };
  const SUBCALL_FIELD_LABELS = {
    mode: '模式',
    question_label: '题位',
    target_index: '目标题号',
    progress_current: '当前进度',
    progress_total: '总题量',
    batch_round: '子批次轮次',
    batch_target_total: '子批次目标题量',
    child_task_id: '子任务ID',
    child_task_name: '子任务名称',
    child_task_status: '子任务状态',
    shard_index: '分片号',
    round: '修复轮次',
    completed_subtasks: '已完成子任务',
    total_subtasks: '总子任务数',
    updated_at: '更新时间',
  };

  const getStatusTagProps = (rawStatus) => {
    const status = String(rawStatus || '').trim().toLowerCase();
    if (!status) return { color: 'default', text: '-' };
    if (['running', 'processing'].includes(status)) return { color: 'processing', text: '进行中' };
    if (['completed', 'success', 'done'].includes(status)) return { color: 'success', text: '已完成' };
    if (['failed', 'error', 'timed_out', 'timeout'].includes(status)) return { color: 'error', text: '失败' };
    if (['partial', 'partial_completed'].includes(status)) return { color: 'warning', text: '部分完成' };
    if (['pending', 'queued'].includes(status)) return { color: 'default', text: '待执行' };
    if (['cancelled', 'canceled'].includes(status)) return { color: 'default', text: '已取消' };
    return { color: 'default', text: status };
  };

  const formatSubcallValue = (key, value) => {
    if (value == null) return '-';
    if (key === 'updated_at') return formatTime(value);
    if (key === 'progress_current' || key === 'progress_total') return String(Number(value || 0));
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (Array.isArray(value)) return value.length ? value.join(', ') : '-';
    if (typeof value === 'object') return stableStringify(value);
    const text = String(value || '').trim();
    return text || '-';
  };

  /** Only treat as critic failed when there is at least one concrete failure reason; avoid defaulting to "failed" before critic has run or when no reason is given. */
  const hasConcreteCriticFailureReason = (criticResult, item) => {
    if (!criticResult || criticResult.passed !== false) return false;
    if (criticResult.reason && String(criticResult.reason).trim()) return true;
    if (criticResult.fix_reason && String(criticResult.fix_reason).trim()) return true;
    if (Array.isArray(criticResult.quality_issues) && criticResult.quality_issues.length > 0) return true;
    if (Array.isArray(criticResult.all_issues) && criticResult.all_issues.length > 0) return true;
    if (item?.critic_details && String(item.critic_details).trim()) return true;
    if (criticResult.leakage_evidence) return true;
    return false;
  };

  const parseQuestionStep = (step) => {
    const msg = String(step?.message || '');
    const detail = String(step?.detail || '');
    if (!msg) return null;
    const phase = msg.startsWith('初稿') ? '初稿' : (msg.startsWith('定稿') ? '定稿' : '题目');
    const phaseRank = phase === '定稿' ? 2 : phase === '初稿' ? 1 : 0;
    if (msg.includes('题干')) return { phase, phaseRank, field: 'stem', detail };
    if (msg.includes('选项')) return { phase, phaseRank, field: 'options', detail };
    if (msg.includes('解析')) return { phase, phaseRank, field: 'explanation', detail };
    if (msg === '题目结果') return { phase, phaseRank, field: 'result', detail };
    return null;
  };
  const splitOptionLines = (detail) => {
    const raw = String(detail || '').trim();
    if (!raw) return [];
    return raw.split(/\s*\|\s*/).map((x) => String(x || '').trim()).filter(Boolean);
  };
  const normalizeOptionLine = (line, idx) => {
    const cleaned = String(line || '').replace(/^\s*[A-Ha-h][\.\、\s]+/, '').trim();
    const key = String.fromCharCode(65 + idx);
    return `${key}. ${cleaned || String(line || '').trim()}`;
  };
  /** Run ID: use backend run_id when present, else infer from router position so "第2轮" writer/critic after reroute show. */
  const getRunIds = (list) => {
    const runIds = [];
    let inferredRun = 0;
    let routerSeen = false;
    (list || []).forEach((step) => {
      if (step && typeof step.run_id === 'number' && step.run_id >= 0) {
        runIds.push(step.run_id);
      } else {
        runIds.push(inferredRun);
      }
      if (step?.node === 'router') {
        if (routerSeen) inferredRun += 1;
        else routerSeen = true;
      }
    });
    return runIds;
  };

  /** Build ordered display list: one merged card per (node, run), so post-reroute writer/critic are visible. */
  const buildOrderedTraceDisplay = (steps, options = {}) => {
    const list = Array.isArray(steps) ? steps : [];
    const latestRunOnly = Boolean(options?.latestRunOnly);
    const runIds = getRunIds(list);
    let latestRunId = 0;
    runIds.forEach((rid) => {
      if (Number.isFinite(rid) && rid > latestRunId) latestRunId = rid;
    });
    const groupByKey = new Map();
    const firstQuestionIdxByKey = new Map();
    list.forEach((step, idx) => {
      const runId = runIds[idx];
      if (latestRunOnly && runId !== latestRunId) return;
      const parsed = parseQuestionStep(step);
      if (!parsed) return;
      const node = step?.node || 'system';
      const key = `${node}_${runId}`;
      if (!firstQuestionIdxByKey.has(key)) firstQuestionIdxByKey.set(key, idx);
      let group = groupByKey.get(key);
      if (!group) {
        group = {
          node,
          runId,
          stem: '',
          stemRank: -1,
          options: '',
          optionsRank: -1,
          explanation: '',
          explanationRank: -1,
          result: '',
        };
        groupByKey.set(key, group);
      }
      const r = parsed.phaseRank;
      if (parsed.field === 'stem' && r >= group.stemRank) {
        group.stem = parsed.detail;
        group.stemRank = r;
      }
      if (parsed.field === 'options' && r >= group.optionsRank) {
        group.options = parsed.detail;
        group.optionsRank = r;
      }
      if (parsed.field === 'explanation' && r >= group.explanationRank) {
        group.explanation = parsed.detail;
        group.explanationRank = r;
      }
      if (parsed.field === 'result') group.result = parsed.detail;
    });
    const ordered = [];
    const emittedRunHeader = new Set();
    const emitRunHeader = (runId) => {
      const rid = Number.isFinite(runId) ? runId : 0;
      if (emittedRunHeader.has(rid)) return;
      emittedRunHeader.add(rid);
      ordered.push({
        type: 'run_header',
        runId: rid,
        label: `第 ${rid + 1} 轮`,
      });
    };
    list.forEach((step, idx) => {
      const runId = runIds[idx];
      if (latestRunOnly && runId !== latestRunId) return;
      const parsed = parseQuestionStep(step);
      if (!parsed) {
        emitRunHeader(runId);
        ordered.push({ type: 'step', step, runId });
        return;
      }
      const node = step?.node || 'system';
      const key = `${node}_${runId}`;
      if (firstQuestionIdxByKey.get(key) !== idx) return;
      const g = groupByKey.get(key);
      if (!g || (!g.stem && !g.options && !g.explanation && !g.result)) return;
      emitRunHeader(runId);
      const runSuffix = runId > 0 ? `（第${runId + 1}轮）` : '';
      ordered.push({
        type: 'merged_card',
        node: g.node,
        nodeLabel: (NODE_LABELS[g.node] || g.node) + runSuffix,
        runId: g.runId,
        stem: g.stem,
        options: g.options,
        explanation: g.explanation,
        result: g.result,
      });
    });
    return ordered;
  };

  const getLatestRunStatus = (steps) => {
    const list = Array.isArray(steps) ? steps : [];
    const runIds = getRunIds(list);
    let latestRunId = 0;
    runIds.forEach((rid) => {
      if (Number.isFinite(rid) && rid > latestRunId) latestRunId = rid;
    });
    let latestRunHasCriticTerminal = false;
    let latestRunCriticOutcome = '';
    let latestRunHasFixer = false;
    let latestRunHasFinalSnapshot = false;
    list.forEach((step, idx) => {
      const rid = runIds[idx];
      if (rid !== latestRunId) return;
      const node = String(step?.node || '');
      const msg = String(step?.message || '');
      if (node === 'fixer') latestRunHasFixer = true;
      if ((node === 'writer' || node === 'fixer') && (
        msg === '作家润色完成'
        || msg.includes('定稿题干')
        || msg.includes('定稿选项')
        || msg.includes('定稿解析')
        || msg === '题目结果'
      )) {
        latestRunHasFinalSnapshot = true;
      }
      if (node === 'critic' && (msg === '审核通过' || msg === '审核驳回')) {
        latestRunHasCriticTerminal = true;
        latestRunCriticOutcome = msg === '审核通过' ? 'passed' : 'failed';
      }
    });
    return {
      latestRunId,
      latestRunHasCriticTerminal,
      latestRunCriticOutcome,
      latestRunHasFixer,
      latestRunHasFinalSnapshot,
    };
  };

  const getQuestionStatus = (item, activeFlag) => {
    const runStatus = getLatestRunStatus(item?.steps || []);
    const criticPassed = (
      item?.critic_result?.passed === true
      || runStatus.latestRunCriticOutcome === 'passed'
    );
    const criticFailed = (
      Boolean(item?.critic_result && hasConcreteCriticFailureReason(item.critic_result, item))
      || runStatus.latestRunCriticOutcome === 'failed'
    );
    if (item?.saved_with_issues) return { color: 'warning', text: '通过（白名单）' };
    if (item?.saved || criticPassed) return { color: 'success', text: '通过' };
    if (criticFailed) return { color: 'error', text: '失败' };
    if (activeFlag) return { color: 'processing', text: '进行中' };
    return { color: 'error', text: '失败' };
  };

  const buildTraceSliceMarkdown = (item) => {
    const content = String(item?.slice_content || '').trim();
    const sid = Number(item?.slice_id || 0);
    const row = approvedSliceById.get(sid);
    const images = Array.isArray(row?.images) ? row.images : [];
    if (!images.length) return content || '（无切片内容）';
    const imageLines = images
      .map((img, idx) => {
        const p = String(img?.image_path || '').trim();
        if (!p) return '';
        const title = String(img?.image_id || '').trim() || p.split('/').pop() || `图片${idx + 1}`;
        const url = getSliceImageUrl(tenantId, p, row?.material_version_id || task?.material_version_id || task?.request?.material_version_id);
        return `- ${title}\n\n  ![${title}](${url})`;
      })
      .filter(Boolean);
    if (!imageLines.length) return content || '（无切片内容）';
    return `${content || ''}\n\n---\n\n### 切片图片\n${imageLines.join('\n\n')}`;
  };

  const formatTime = (value) => {
    const s = String(value || '').trim();
    if (!s) return '-';
    // 将 ISO 时间戳转成更易读的「YYYY-MM-DD HH:mm:ss」形式，去掉毫秒和时区
    return s.replace('T', ' ').replace(/\.\d+\+\d{2}:\d{2}$/, '');
  };

  const calcDuration = (start, end) => {
    const s = Date.parse(start || '');
    const e = Date.parse(end || '');
    if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return '-';
    const diffMs = e - s;
    const totalSec = Math.floor(diffMs / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const sec = totalSec % 60;
    if (h > 0) return `${h}小时${m}分${sec}秒`;
    if (m > 0) return `${m}分${sec}秒`;
    return `${sec}秒`;
  };

  const formatDurationMs = (value) => {
    const diffMs = Number(value || 0);
    if (!Number.isFinite(diffMs) || diffMs <= 0) return '0秒';
    const totalSec = Math.floor(diffMs / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const sec = totalSec % 60;
    if (h > 0) return `${h}小时${m}分${sec}秒`;
    if (m > 0) return `${m}分${sec}秒`;
    return `${sec}秒`;
  };

  const timingMetrics = useMemo(() => {
    const taskStartMs = Date.parse(task?.started_at || '');
    const taskEndMs = Date.parse(task?.ended_at || '');
    const effectiveTaskEndMs = (
      Number.isFinite(taskEndMs)
      ? taskEndMs
      : (isTaskActive ? nowMs : NaN)
    );
    const totalDurationMs = (
      Number.isFinite(taskStartMs)
      && Number.isFinite(effectiveTaskEndMs)
      && effectiveTaskEndMs >= taskStartMs
    ) ? (effectiveTaskEndMs - taskStartMs) : 0;
    const questionElapsedMsSum = processTrace.reduce(
      (sum, item) => sum + Math.max(0, Number(item?.elapsed_ms || 0)),
      0,
    );
    const timingUnknown = processTrace.length > 0 && processTrace.every((item) => Boolean(item?.timing_unknown));
    const taskOverheadMs = Math.max(0, totalDurationMs - questionElapsedMsSum);
    return {
      totalDurationMs,
      questionElapsedMsSum,
      taskOverheadMs,
      timingUnknown,
    };
  }, [isTaskActive, nowMs, processTrace, task?.ended_at, task?.started_at]);

  const derivedProgress = useMemo(() => {
    const rawProgress = task?.progress && typeof task.progress === 'object' ? task.progress : {};
    const requestedTotal = Number(task?.request?.num_questions || 0);
    const total = Math.max(Number(rawProgress?.total || 0), requestedTotal, processTrace.length);
    const current = isTaskActive
      ? Math.max(Number(rawProgress?.current || 0), processTrace.length)
      : Math.max(Number(rawProgress?.current || 0), Number(rawProgress?.total || 0), processTrace.length);
    return { current, total };
  }, [isTaskActive, processTrace.length, task?.progress, task?.request?.num_questions]);

  const currentQuestionLabel = useMemo(() => {
    if (!isTaskActive) return '-';
    const subcallQuestionLabel = String(currentSubcall?.question_label || task?.current_question_id || '').trim();
    if (subcallQuestionLabel) return subcallQuestionLabel;
    const liveItem = processTrace.find((item) => !item?.elapsed_ms) || processTrace[processTrace.length - 1];
    const index = Number(liveItem?.target_index || liveItem?.index || 0);
    return index > 0 ? `第 ${index} 题` : '-';
  }, [currentSubcall, isTaskActive, processTrace, task?.current_question_id]);

  const currentSubcallEntries = useMemo(() => {
    const entries = [];
    const seen = new Set();
    Object.entries(SUBCALL_FIELD_LABELS).forEach(([key, label]) => {
      const raw = currentSubcall?.[key];
      const formatted = formatSubcallValue(key, raw);
      if (formatted === '-') return;
      seen.add(key);
      entries.push({ key, label, value: formatted });
    });
    Object.entries(currentSubcall || {}).forEach(([key, raw]) => {
      if (seen.has(key)) return;
      const formatted = formatSubcallValue(key, raw);
      if (formatted === '-') return;
      entries.push({ key, label: key, value: formatted });
    });
    return entries;
  }, [currentSubcall]);

  const hasRunDiagnostics = (
    subtasks.length > 0
    || repairRounds.length > 0
    || sliceFailureStats.length > 0
    || subtaskCount > 0
    || repairRoundCount > 0
    || failureSliceCount > 0
  );
  const liveSubtaskTraces = useMemo(
    () => (Array.isArray(task?.live_subtask_traces) ? task.live_subtask_traces : []),
    [task],
  );
  const autoBankEnabled = Boolean(task?.request?.persist_to_bank ?? task?.request?.save_to_bank ?? true);
  const effectiveProcessTrace = useMemo(() => {
    const hasParentTrace = processTrace.some((row) => row && typeof row === 'object');
    if (!hasParentTrace && liveSubtaskTraces.length > 0) {
      const flattened = [];
      liveSubtaskTraces.forEach((sub) => {
        const rows = Array.isArray(sub?.process_trace) ? sub.process_trace : [];
        const subTaskId = String(sub?.task_id || '');
        rows.forEach((row, idx) => {
          if (!row || typeof row !== 'object') return;
          const item = { ...row };
          item._subtask_id = subTaskId;
          item._subtask_name = String(sub?.task_name || '');
          item._subtask_local_index = idx + 1;
          flattened.push(item);
        });
      });
      flattened.sort((a, b) => {
        const ta = Number(a?.target_index || a?.index || 0);
        const tb = Number(b?.target_index || b?.index || 0);
        if (ta !== tb) return ta - tb;
        const sa = String(a?._subtask_id || '');
        const sb = String(b?._subtask_id || '');
        if (sa !== sb) return sa.localeCompare(sb);
        return Number(a?._subtask_local_index || 0) - Number(b?._subtask_local_index || 0);
      });
      return flattened;
    }
    const byTarget = new Map();
    processTrace.forEach((row, idx) => {
      if (!row || typeof row !== 'object') return;
      const key = Number(row?.target_index || row?.index || idx + 1);
      if (!key) return;
      byTarget.set(key, row);
    });
    liveSubtaskTraces.forEach((sub) => {
      const rows = Array.isArray(sub?.process_trace) ? sub.process_trace : [];
      rows.forEach((row, idx) => {
        if (!row || typeof row !== 'object') return;
        const key = Number(row?.target_index || row?.index || idx + 1);
        if (!key) return;
        const prev = byTarget.get(key);
        const prevSaved = Boolean(prev?.saved);
        const nextSaved = Boolean(row?.saved);
        const prevSteps = Array.isArray(prev?.steps) ? prev.steps.length : 0;
        const nextSteps = Array.isArray(row?.steps) ? row.steps.length : 0;
        if (!prev || (!prevSaved && (nextSaved || nextSteps >= prevSteps))) {
          byTarget.set(key, row);
        }
      });
    });
    return Array.from(byTarget.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([, row]) => row);
  }, [processTrace, liveSubtaskTraces]);
  const placeholderOnlyTrace = useMemo(() => {
    if (!effectiveProcessTrace.length) return false;
    return effectiveProcessTrace.every((row) => String(row?.question_id || '').startsWith('live:'));
  }, [effectiveProcessTrace]);
  const recoveredSubtasksWithoutTrace = useMemo(() => {
    return subtasks.filter((sub) => {
      const saved = Number(sub?.saved_count || 0);
      const taskName = String(sub?.task_name || '').trim();
      if (saved <= 0 || !taskName) return false;
      const hasLiveTrace = liveSubtaskTraces.some((item) => String(item?.task_name || '').trim() === taskName && Array.isArray(item?.process_trace) && item.process_trace.length > 0);
      return !hasLiveTrace;
    });
  }, [liveSubtaskTraces, subtasks]);

  const traceSuccessCount = useMemo(
    () => countTraceSuccess(effectiveProcessTrace),
    [effectiveProcessTrace]
  );
  const itemCount = items.length;
  const apiGeneratedCount = Number(task?.generated_count || 0);
  const subtaskGeneratedCount = subtasks.reduce((sum, sub) => sum + Number(sub?.generated_count || 0), 0);
  const subtaskSavedCount = subtasks.reduce((sum, sub) => sum + Number(sub?.saved_count || 0), 0);
  const displayGeneratedCount = Math.max(apiGeneratedCount, traceSuccessCount, itemCount, subtaskGeneratedCount);
  const displaySavedCount = Math.max(Number(task?.saved_count || 0), itemCount, subtaskSavedCount);
  const generatedCountMismatch = (
    apiGeneratedCount !== traceSuccessCount
    || apiGeneratedCount !== itemCount
    || traceSuccessCount !== itemCount
    || apiGeneratedCount !== subtaskGeneratedCount
  );

  useEffect(() => {
    if (!tenantId || !taskId || !isTaskActive) return undefined;
    const timer = setInterval(() => {
      loadDetail({ silent: true });
    }, 1500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, taskId, isTaskActive]);

  useEffect(() => {
    if (!isTaskActive) return undefined;
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isTaskActive]);

  const columns = [
    { title: '题干', dataIndex: '题干', ellipsis: true },
    { title: '答案', dataIndex: '正确答案', width: 100, render: (v) => <Tag color="green">{v}</Tag> },
    { title: '难度值', dataIndex: '难度值', width: 100 },
    { title: '来源切片', dataIndex: '来源路径', ellipsis: true },
    {
      title: '查看',
      width: 100,
      render: (_, record) => (
        <Button
          size="small"
          onClick={() => {
            setViewQuestionRecord(record || {});
            setViewQuestionOpen(true);
          }}
        >
          查看
        </Button>
      ),
    },
  ];

  const subtaskColumns = [
    { title: '子任务', dataIndex: 'task_name', ellipsis: true },
    {
      title: '类型',
      width: 100,
      render: (_, record) => {
        const kind = String(record?.kind || '');
        if (kind === 'repair') return <Tag color="orange">{`repair#${Number(record?.round || 0)}`}</Tag>;
        if (kind === 'shard') return <Tag color="blue">{`分片#${Number(record?.shard_index || 0)}`}</Tag>;
        if (kind === 'resume') return <Tag color="purple">{`续跑#${Number(record?.round || 0)}`}</Tag>;
        return <Tag>{kind || '-'}</Tag>;
      },
    },
    {
      title: '本轮目标数',
      width: 100,
      render: (_, record) => {
        const total = Number(record?.target_total || 0);
        return total > 0 ? String(total) : '-';
      },
    },
    {
      title: '状态',
      width: 100,
      render: (_, record) => {
        const { color, text } = getStatusTagProps(record?.status);
        return <Tag color={color}>{text}</Tag>;
      },
    },
    { title: '本轮生成/入库', width: 140, render: (_, record) => `${Number(record?.generated_count || 0)} / ${Number(record?.saved_count || 0)}` },
    { title: '错误数', dataIndex: 'error_count', width: 90 },
    { title: '开始时间', dataIndex: 'started_at', width: 170, render: formatTime },
    { title: '结束时间', dataIndex: 'ended_at', width: 170, render: formatTime },
  ];

  const repairColumns = [
    { title: '轮次', dataIndex: 'round', width: 80, render: (v) => `第${Number(v || 0)}轮` },
    { title: '策略', dataIndex: 'strategy', width: 180, ellipsis: true, render: (v) => String(v || '-') || '-' },
    { title: '修复位次', dataIndex: 'targets', ellipsis: true, render: (v) => (Array.isArray(v) && v.length ? v.join(', ') : '-') },
    { title: '子任务数', dataIndex: 'subtask_count', width: 90 },
    { title: '生成/入库', width: 120, render: (_, record) => `${Number(record?.generated_count || 0)} / ${Number(record?.saved_count || 0)}` },
    { title: '错误数', dataIndex: 'error_count', width: 90 },
    {
      title: '状态',
      width: 100,
      render: (_, record) => {
        const { color, text } = getStatusTagProps(record?.status);
        return <Tag color={color}>{text}</Tag>;
      },
    },
  ];

  const sliceFailureColumns = [
    { title: '切片ID', dataIndex: 'slice_id', width: 90 },
    { title: '尝试数', dataIndex: 'attempt_count', width: 90 },
    { title: '失败数', dataIndex: 'fail_count', width: 90 },
    { title: '通过数', dataIndex: 'pass_count', width: 90 },
    { title: '带问题入库', dataIndex: 'saved_with_issues_count', width: 110 },
    { title: '最新目标题位', dataIndex: 'latest_target_index', width: 110 },
    { title: '最新失败类型', dataIndex: 'last_fail_types', ellipsis: true, render: (v) => (Array.isArray(v) && v.length ? v.join(', ') : '-') },
    { title: '路径', dataIndex: 'latest_path', ellipsis: true },
  ];

  const renderTraceCollapse = (traceRows, activeFlag, keyPrefix) => (
    <Collapse
      defaultActiveKey={traceRows.length > 0 ? [`${keyPrefix}_${String(traceRows[0]?.index || 0)}`] : []}
      items={traceRows.map((item, itemIdx) => {
        const qStatus = getQuestionStatus(item, activeFlag);
        const displayIndex = Number(item?.target_index || item?.index || itemIdx + 1);
        return {
          key: `${keyPrefix}_${String(item.index || displayIndex || itemIdx)}`,
          label: (
            <Space>
              <span>{`第 ${displayIndex} 题 | 切片 ${item.slice_id} | 耗时 ${item?.timing_unknown ? '恢复数据' : `${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`}`}</span>
              <Tag color={qStatus.color}>{qStatus.text}</Tag>
            </Space>
          ),
          children: (
            <Space direction="vertical" style={{ width: '100%' }} size={12}>
              {(() => {
                const status = getLatestRunStatus(item.steps || []);
                if (!activeFlag || status.latestRunId <= 0 || status.latestRunHasCriticTerminal) return null;
                return (
                  <Alert
                    type="info"
                    showIcon
                    message={`当前题已进入第 ${status.latestRunId + 1} 轮重试`}
                    description="为避免把上一轮 writer/fixer 定稿和当前轮 specialist/calculator 初稿混在一起，步骤流水默认只展示当前轮。任务结束后会恢复展示完整历史。"
                  />
                );
              })()}
              <Typography.Text type="secondary">{item.slice_path || '（无路径）'}</Typography.Text>
              <div style={{ maxHeight: 260, overflow: 'auto' }}>
                <MarkdownWithMermaid text={buildTraceSliceMarkdown({ ...item, slice_content: formatSliceContent(item.slice_content || '') })} />
              </div>
              <div>
                <Typography.Text strong style={{ display: 'block', marginBottom: 6 }}>步骤流水</Typography.Text>
                <Space direction="vertical" style={{ width: '100%' }} size={12}>
                  {(() => {
                    const status = getLatestRunStatus(item.steps || []);
                    const latestRunOnly = Boolean(activeFlag && status.latestRunId > 0 && !status.latestRunHasCriticTerminal);
                    return buildOrderedTraceDisplay(item.steps || [], { latestRunOnly });
                  })().map((entry, idx) => {
                    if (entry.type === 'run_header') {
                      return (
                        <Alert
                          key={`${keyPrefix}_${displayIndex}_run_${entry.runId}_${idx}`}
                          type="info"
                          showIcon={false}
                          message={`${entry.label}流程`}
                        />
                      );
                    }
                    if (entry.type === 'step') {
                      const step = entry.step;
                      return (
                        <div key={`${keyPrefix}_${displayIndex}_step_${idx}`} style={{ marginBottom: 4 }}>
                          <Typography.Text type="secondary">[{step?.node || 'system'}] {step?.message || ''}</Typography.Text>
                          {step?.detail ? (
                            <Typography.Text style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', display: 'block', marginLeft: 12, marginTop: 2 }}>
                              {step.detail}
                            </Typography.Text>
                          ) : null}
                        </div>
                      );
                    }
                    const card = entry;
                    return (
                      <Card
                        key={`${keyPrefix}_${displayIndex}_card_${card.node}_${idx}`}
                        size="small"
                        title={`${card.nodeLabel}（${card.node}）`}
                        style={{ background: '#fafcff' }}
                      >
                        <Space direction="vertical" style={{ width: '100%' }} size={8}>
                          {card.stem ? (
                            <div>
                              <Typography.Text strong type="secondary" style={{ fontSize: 12 }}>题干</Typography.Text>
                              <Typography.Paragraph style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                {card.stem}
                              </Typography.Paragraph>
                            </div>
                          ) : null}
                          {card.options ? (
                            <div>
                              <Typography.Text strong type="secondary" style={{ fontSize: 12 }}>选项</Typography.Text>
                              <div style={{ marginTop: 4 }}>
                                {splitOptionLines(card.options).map((line, i) => (
                                  <Typography.Text key={i} style={{ display: 'block' }}>
                                    {normalizeOptionLine(line, i)}
                                  </Typography.Text>
                                ))}
                              </div>
                            </div>
                          ) : null}
                          {card.explanation ? (
                            <div>
                              <Typography.Text strong type="secondary" style={{ fontSize: 12 }}>解析</Typography.Text>
                              <Typography.Paragraph style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                {card.explanation}
                              </Typography.Paragraph>
                            </div>
                          ) : null}
                          {card.result ? (
                            <Typography.Text type="secondary">{card.result}</Typography.Text>
                          ) : null}
                        </Space>
                      </Card>
                    );
                  })}
                </Space>
              </div>
              {(() => {
                const status = getLatestRunStatus(item.steps || []);
                if (!activeFlag || status.latestRunHasCriticTerminal) return null;
                return (
                  <Alert
                    type="info"
                    showIcon
                    message={`当前为第 ${status.latestRunId + 1} 轮，尚未到 Critic 审核结果`}
                    description={
                      status.latestRunHasFixer
                        ? '本轮已进入过 Fixer，后续将继续显示当前轮次的 Critic 结果。'
                        : '若你看到下方 Critic 结果，可能是上一轮结果，当前轮次结果尚未返回。'
                    }
                  />
                );
              })()}
              {item.critic_result && (item.critic_result.passed === true || hasConcreteCriticFailureReason(item.critic_result, item)) && (
                (() => {
                  const status = getLatestRunStatus(item.steps || []);
                  if (activeFlag && !status.latestRunHasCriticTerminal) return null;
                  return (
                    <Card size="small" title="Critic 结果" style={{ background: item.critic_result.passed ? '#f6ffed' : '#fff2f0' }}>
                      <Space direction="vertical" style={{ width: '100%' }} size={6}>
                        <div>
                          <Typography.Text strong>结果：</Typography.Text>
                          <Tag color={item.critic_result.passed ? 'success' : 'error'}>
                            {item.critic_result.passed ? '通过' : '未通过'}
                          </Tag>
                          {item.critic_result.issue_type && <Tag>issue_type={item.critic_result.issue_type}</Tag>}
                          {item.critic_result.fix_strategy && <Tag>修复策略={item.critic_result.fix_strategy}</Tag>}
                        </div>
                        {!item.critic_result.passed && !item.critic_result.reason && !item.critic_result.fix_reason
                          && !(Array.isArray(item.critic_result.quality_issues) && item.critic_result.quality_issues.length > 0)
                          && !(Array.isArray(item.critic_result.all_issues) && item.critic_result.all_issues.length > 0) ? (
                          item.critic_details ? (
                            <Typography.Paragraph style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                              {item.critic_details}
                            </Typography.Paragraph>
                          ) : (
                            <Typography.Text type="secondary" style={{ fontStyle: 'italic' }}>
                              （暂无详细说明，可能为规则校验或反向解题未通过；请查看上方步骤流水中的审核驳回详情）
                            </Typography.Text>
                          )
                        ) : null}
                        {item.critic_result.reason ? (
                          <div>
                            <Typography.Text strong>原因/说明：</Typography.Text>
                            <Typography.Paragraph style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                              {item.critic_result.reason}
                            </Typography.Paragraph>
                          </div>
                        ) : null}
                      </Space>
                    </Card>
                  );
                })()
              )}
              {item.final_json && (() => {
                const status = getLatestRunStatus(item.steps || []);
                if (activeFlag && status.latestRunId > 0 && !status.latestRunHasCriticTerminal && !status.latestRunHasFinalSnapshot) {
                  return null;
                }
                const previewMeta = getFinalQuestionPreviewCardMeta(item);
                return (
                  <Card
                    size="small"
                    title={(
                      <Space wrap>
                        <span>{previewMeta.title}</span>
                        <Tag color={previewMeta.tagColor}>{previewMeta.tag}</Tag>
                        <Tooltip title={previewMeta.tooltip}>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>(?)</Typography.Text>
                        </Tooltip>
                      </Space>
                    )}
                  >
                    <QuestionDetailView question={item.final_json} />
                  </Card>
                );
              })()}
            </Space>
          ),
        };
      })}
    />
  );

  /**
   * 计算子任务预计产出题量（优先使用目标区间）。
   * @param {Record<string, any>} sub
   * @returns {number}
   */
  const estimateSubtaskQuestionCount = (sub) => {
    const targetTotal = Number(sub?.target_total || 0);
    if (targetTotal > 0) return targetTotal;
    const start = Number(sub?.target_start || 0);
    const end = Number(sub?.target_end || 0);
    if (start > 0 && end >= start) return end - start + 1;
    const rows = Array.isArray(sub?.process_trace) ? sub.process_trace : [];
    if (rows.length > 0) return rows.length;
    return 0;
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Card>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Typography.Title level={5} style={{ margin: 0 }}>出题任务详情</Typography.Title>
          <Space>
            {['pending', 'running'].includes(String(task?.status || '')) && (
              <Button
                danger
                loading={cancelling}
                onClick={async () => {
                  if (!tenantId || !taskId) return;
                  setCancelling(true);
                  try {
                    const res = await cancelGenerateTask(tenantId, taskId);
                    message.success(res?.message || '已请求取消');
                    await loadDetail();
                  } catch (e) {
                    message.error(e?.response?.data?.error?.message || '取消失败');
                  } finally {
                    setCancelling(false);
                  }
                }}
              >
                取消任务
              </Button>
            )}
            <Button onClick={() => loadDetail()} loading={loading}>刷新</Button>
            <Button type="primary" onClick={() => navigate('/ai-generate')}>返回任务列表</Button>
          </Space>
        </Space>
      </Card>

      <Card loading={loading}>
        <Alert
          type={autoBankEnabled ? 'success' : 'warning'}
          showIcon
          style={{ marginBottom: 12 }}
          message={autoBankEnabled
            ? '当前任务：自动入库已开启（通过题将进入题库）'
            : '当前任务：自动入库已关闭（通过题不会进入题库）'}
          description={(
            <Space wrap>
              <Typography.Text>
                {autoBankEnabled
                  ? '如需停止入库，可切换为“关闭自动入库”，系统会撤回该任务（含子任务）已入库题。'
                  : '如需补入当前已通过题，可切换为“开启自动入库”，系统会立即补入去重后的通过题。'}
              </Typography.Text>
              <Switch
                checkedChildren="自动入库"
                unCheckedChildren="不入库"
                checked={autoBankEnabled}
                loading={updatingBankPolicy}
                onChange={(checked) => {
                  Modal.confirm({
                    title: checked ? '开启自动入库？' : '关闭自动入库？',
                    content: checked
                      ? '开启后：当前任务已通过题会立即补入题库，后续通过题也会继续入库。'
                      : '关闭后：当前任务（含子任务）已入库题会从题库移除，后续通过题也不再入库。',
                    okText: '确认',
                    cancelText: '取消',
                    onOk: async () => {
                      if (!tenantId || !taskId) return;
                      setUpdatingBankPolicy(true);
                      try {
                        const res = await updateGenerateTaskBankPolicy(tenantId, taskId, { enabled: checked });
                        message.success(
                          checked
                            ? `已开启自动入库：补入 ${Number(res?.added || 0)} 题，当前入库 ${Number(res?.saved_count || 0)} 题`
                            : `已关闭自动入库：移除 ${Number(res?.removed || 0)} 题，当前入库 ${Number(res?.saved_count || 0)} 题`
                        );
                        await loadDetail({ silent: true, forceSliceReload: false });
                      } catch (e) {
                        message.error(e?.response?.data?.error?.message || e?.message || '更新任务入库策略失败');
                      } finally {
                        setUpdatingBankPolicy(false);
                      }
                    },
                  });
                }}
              />
            </Space>
          )}
        />
        <Descriptions size="small" bordered column={2}>
          <Descriptions.Item label="任务ID">{String(task?.task_id || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="任务名称">{String(task?.task_name || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="状态">{String(task?.status || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="创建人">{String(task?.creator || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="创建时间">{formatTime(task?.created_at)}</Descriptions.Item>
          <Descriptions.Item label="开始时间">{formatTime(task?.started_at)}</Descriptions.Item>
          <Descriptions.Item label="结束时间">{formatTime(task?.ended_at)}</Descriptions.Item>
          <Descriptions.Item label="任务总耗时">
            {calcDuration(task?.started_at, task?.ended_at || (isTaskActive ? new Date(nowMs).toISOString() : ''))}
          </Descriptions.Item>
          <Descriptions.Item label="单题耗时合计">{timingMetrics.timingUnknown ? '恢复数据，无法精确还原' : formatDurationMs(timingMetrics.questionElapsedMsSum)}</Descriptions.Item>
          <Descriptions.Item label="任务级开销">{timingMetrics.timingUnknown ? '-' : formatDurationMs(timingMetrics.taskOverheadMs)}</Descriptions.Item>
          <Descriptions.Item label="生成结果">{`${displayGeneratedCount} / 入库 ${displaySavedCount}`}</Descriptions.Item>
          <Descriptions.Item label="进度">{`${derivedProgress.current}/${derivedProgress.total}`}</Descriptions.Item>
          <Descriptions.Item label="当前题目">{currentQuestionLabel}</Descriptions.Item>
          <Descriptions.Item label="当前节点">{String(task?.current_node || '').trim() || '-'}</Descriptions.Item>
          <Descriptions.Item label="当前子调用" span={2}>
            {currentSubcallEntries.length > 0 ? (
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                {currentSubcallEntries.map((entry) => (
                  <Typography.Text key={entry.key}>
                    {`${entry.label}：${entry.value}`}
                  </Typography.Text>
                ))}
              </Space>
            ) : '-'}
          </Descriptions.Item>
          {task?.batch_metrics && (
            <>
              <Descriptions.Item label="总成本">
                {formatAmount(task.batch_metrics.total_cost, task.batch_metrics.currency)}
              </Descriptions.Item>
              <Descriptions.Item label="平均成本/题（毛）">
                {formatAmount(task.batch_metrics.avg_cost_per_question, task.batch_metrics.currency)}
              </Descriptions.Item>
              <Descriptions.Item label="CPVQ（单题有效成本）">
                {task.batch_metrics.cpvq != null
                  ? formatAmount(task.batch_metrics.cpvq, task.batch_metrics.cpvq_currency || task.batch_metrics.currency)
                  : '—'}
              </Descriptions.Item>
            </>
          )}
        </Descriptions>
      </Card>

      {hasRunDiagnostics && (
        <Card title="运行记录">
          <Space direction="vertical" style={{ width: '100%' }} size={16}>
            <Alert
              type="info"
              showIcon
              message={`父任务累计结果：已生成 ${Number(task?.generated_count || 0)} 题，已入库 ${Number(task?.saved_count || 0)} 题。`}
              description="下方子任务表展示的是每一轮续跑/分片子任务各自的本轮产出，不是父任务累计结果。"
            />
            {subtasks.length === 0 && repairRounds.length === 0 && sliceFailureStats.length === 0 && (
              <Alert
                type="info"
                showIcon
                message="运行记录摘要已生成，明细仍在加载"
                description={`当前摘要：子任务 ${subtaskCount} 个，修复轮次 ${repairRoundCount} 个，失败切片 ${failureSliceCount} 个。`}
              />
            )}
            {subtasks.length > 0 && (
              <div>
                <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>子任务</Typography.Text>
                <Table
                  rowKey={(record) => `${record?.task_name || 'sub'}_${record?.run_id || ''}`}
                  columns={subtaskColumns}
                  dataSource={subtasks}
                  pagination={false}
                  size="small"
                />
              </div>
            )}
            {repairRounds.length > 0 && (
              <div>
                <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>修复轮次</Typography.Text>
                <Table
                  rowKey={(record) => `repair_${record?.round || 0}`}
                  columns={repairColumns}
                  dataSource={repairRounds}
                  pagination={false}
                  size="small"
                />
              </div>
            )}
            {sliceFailureStats.length > 0 && (
              <div>
                <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>失败切片统计</Typography.Text>
                <Table
                  rowKey={(record) => `slice_${record?.slice_id || 0}`}
                  columns={sliceFailureColumns}
                  dataSource={sliceFailureStats}
                  pagination={{ pageSize: 10 }}
                  size="small"
                />
              </div>
            )}
          </Space>
        </Card>
      )}

      {liveSubtaskTraces.length > 0 && (
        <Card title="活跃子任务过程">
          <Space direction="vertical" style={{ width: '100%' }} size={16}>
            {liveSubtaskTraces.map((sub, subIdx) => {
              const statusProps = getStatusTagProps(sub?.status);
              const estimatedCount = estimateSubtaskQuestionCount(sub);
              const title = `子任务 ${subIdx + 1}${estimatedCount > 0 ? `（预计产出 ${estimatedCount} 题）` : ''}`;
              return (
                <Card
                  key={String(sub?.task_id || '')}
                  size="small"
                  title={title}
                  extra={(
                    <Space>
                      <Tag color={statusProps.color}>{statusProps.text}</Tag>
                      <Typography.Text type="secondary">{String(sub?.task_name || '').trim() || '-'}</Typography.Text>
                    </Space>
                  )}
                >
                  {Array.isArray(sub?.process_trace) && sub.process_trace.length > 0 ? (
                    renderTraceCollapse(
                      sub.process_trace,
                      ['pending', 'running'].includes(String(sub?.status || '')),
                      `sub_${String(sub?.task_id || '')}`,
                    )
                  ) : (
                    <Alert
                      type="info"
                      showIcon
                      message="子任务已创建，步骤流水尚未返回"
                      description={String(sub?.task_id || '').trim()}
                    />
                  )}
                </Card>
              );
            })}
          </Space>
        </Card>
      )}

      <Card title="任务过程">
        {generatedCountMismatch && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="生成统计口径存在差异，已按较大值展示任务生成结果"
            description={`任务字段 generated_count=${apiGeneratedCount}，过程成功数=${traceSuccessCount}，题目结果条数=${itemCount}，子任务生成数合计=${subtaskGeneratedCount}。`}
          />
        )}
        {renderTraceCollapse(effectiveProcessTrace, isTaskActive, 'parent')}
      </Card>

      <Card title="题目结果">
        <Table
          rowKey={(record, idx) => `${taskId || 'task'}_${idx}`}
          columns={columns}
          dataSource={items}
          pagination={{ pageSize: 10 }}
        />
      </Card>

      {errors.length > 0 && (
        <Card title="错误信息">
          <Space direction="vertical" style={{ width: '100%' }}>
            {errors.map((e, i) => (
              <Alert key={i} type="error" message={String(e)} />
            ))}
          </Space>
        </Card>
      )}

      <Modal
        open={viewQuestionOpen}
        title="题目详情"
        width={900}
        footer={null}
        onCancel={() => setViewQuestionOpen(false)}
        destroyOnClose
      >
        <QuestionDetailView question={viewQuestionRecord || {}} />
      </Modal>

    </Space>
  );
}
