import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { useNavigate, useParams } from 'react-router-dom';
import { cancelGenerateTask, getGenerateTask, getSlices, getSliceImageUrl } from '../services/api';
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
        || prev.progress !== merged.progress
      )
    );
    return changed ? merged : prev;
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadDetail = async ({ silent = false, forceSliceReload = false } = {}) => {
    if (!tenantId || !taskId) return;
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
      message.error(e?.response?.data?.error?.message || '加载任务详情失败');
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

  const NODE_LABELS = { router: '路由', specialist: '初稿', writer: '作家', critic: '审核', fixer: '修复', calculator: '计算', system: '系统' };

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
      }
    });
    return {
      latestRunId,
      latestRunHasCriticTerminal,
      latestRunHasFixer,
      latestRunHasFinalSnapshot,
    };
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
    const totalDurationMs = (
      Number.isFinite(taskStartMs)
      && Number.isFinite(taskEndMs)
      && taskEndMs >= taskStartMs
    ) ? (taskEndMs - taskStartMs) : 0;
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
  }, [processTrace, task?.ended_at, task?.started_at]);

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
    const liveItem = processTrace.find((item) => !item?.elapsed_ms) || processTrace[processTrace.length - 1];
    const index = Number(liveItem?.index || 0);
    return index > 0 ? `第 ${index} 题` : '-';
  }, [isTaskActive, processTrace]);

  useEffect(() => {
    if (!tenantId || !taskId || !isTaskActive) return undefined;
    const timer = setInterval(() => {
      loadDetail({ silent: true });
    }, 1500);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, taskId, isTaskActive]);

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
        <Descriptions size="small" bordered column={2}>
          <Descriptions.Item label="任务ID">{String(task?.task_id || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="任务名称">{String(task?.task_name || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="状态">{String(task?.status || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="创建人">{String(task?.creator || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="创建时间">{formatTime(task?.created_at)}</Descriptions.Item>
          <Descriptions.Item label="开始时间">{formatTime(task?.started_at)}</Descriptions.Item>
          <Descriptions.Item label="结束时间">{formatTime(task?.ended_at)}</Descriptions.Item>
          <Descriptions.Item label="任务总耗时">{calcDuration(task?.started_at, task?.ended_at)}</Descriptions.Item>
          <Descriptions.Item label="单题耗时合计">{timingMetrics.timingUnknown ? '恢复数据，无法精确还原' : formatDurationMs(timingMetrics.questionElapsedMsSum)}</Descriptions.Item>
          <Descriptions.Item label="任务级开销">{timingMetrics.timingUnknown ? '-' : formatDurationMs(timingMetrics.taskOverheadMs)}</Descriptions.Item>
          <Descriptions.Item label="生成结果">{`${Number(task?.generated_count || 0)} / 入库 ${Number(task?.saved_count || 0)}`}</Descriptions.Item>
          <Descriptions.Item label="进度">{`${derivedProgress.current}/${derivedProgress.total}`}</Descriptions.Item>
          <Descriptions.Item label="当前题目">{currentQuestionLabel}</Descriptions.Item>
          <Descriptions.Item label="当前节点">{String(task?.current_node || '').trim() || '-'}</Descriptions.Item>
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

      <Card title="任务过程">
        <Collapse
          defaultActiveKey={processTrace.length > 0 ? [String(processTrace[0].index)] : []}
          items={processTrace.map((item) => {
            const criticFailed = item.critic_result && hasConcreteCriticFailureReason(item.critic_result, item);
            const statusLabel = item.saved ? '通过' : (criticFailed ? '未通过' : '—');
            const statusColor = item.saved ? 'green' : (statusLabel === '未通过' ? 'red' : 'default');
            return {
              key: String(item.index),
              label: (
                <Space>
                  <span>{`第 ${item.index} 题 | 切片 ${item.slice_id} | 耗时 ${item?.timing_unknown ? '恢复数据' : `${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`}`}</span>
                  <Tag color={statusColor}>{statusLabel}</Tag>
                </Space>
              ),
          children: (
                <Space direction="vertical" style={{ width: '100%' }} size={12}>
                  {(() => {
                    const status = getLatestRunStatus(item.steps || []);
                    if (!isTaskActive || status.latestRunId <= 0 || status.latestRunHasCriticTerminal) return null;
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
                        const latestRunOnly = Boolean(isTaskActive && status.latestRunId > 0 && !status.latestRunHasCriticTerminal);
                        return buildOrderedTraceDisplay(item.steps || [], { latestRunOnly });
                      })().map((entry, idx) => {
                        if (entry.type === 'run_header') {
                          return (
                            <Alert
                              key={`${item.index}_run_${entry.runId}_${idx}`}
                              type="info"
                              showIcon={false}
                              message={`${entry.label}流程`}
                            />
                          );
                        }
                        if (entry.type === 'step') {
                          const step = entry.step;
                          return (
                            <div key={`${item.index}_step_${idx}`} style={{ marginBottom: 4 }}>
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
                            key={`${item.index}_card_${card.node}_${idx}`}
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
                    if (!isTaskActive || status.latestRunHasCriticTerminal) return null;
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
                      // Running state: hide stale critic result from previous run until current run reaches critic terminal step.
                      if (isTaskActive && !status.latestRunHasCriticTerminal) return null;
                      return (
                    <Card size="small" title="Critic 结果" style={{ background: item.critic_result.passed ? '#f6ffed' : '#fff2f0' }}>
                      <Space direction="vertical" style={{ width: '100%' }} size={6}>
                        <div>
                          <Typography.Text strong>结果：</Typography.Text>
                          <Tag color={item.critic_result.passed ? 'success' : 'error'}>
                            {item.critic_result.passed ? '通过' : '未通过'}
                          </Tag>
                          {item.critic_result.issue_type && (
                            <Tag>issue_type={item.critic_result.issue_type}</Tag>
                          )}
                          {item.critic_result.fix_strategy && (
                            <Tag>修复策略={item.critic_result.fix_strategy}</Tag>
                          )}
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
                        {item.critic_result.reason && (
                          <div>
                            <Typography.Text strong>原因/说明：</Typography.Text>
                            <Typography.Paragraph style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                              {item.critic_result.reason}
                            </Typography.Paragraph>
                          </div>
                        )}
                        {item.critic_result.fix_reason && (
                          <div>
                            <Typography.Text strong>修复建议：</Typography.Text>
                            <Typography.Paragraph style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap' }}>
                              {item.critic_result.fix_reason}
                            </Typography.Paragraph>
                          </div>
                        )}
                        {Array.isArray(item.critic_result.quality_issues) && item.critic_result.quality_issues.length > 0 && (
                          <div>
                            <Typography.Text strong>质量问题：</Typography.Text>
                            <ul style={{ margin: '4px 0 0 0', paddingLeft: 18 }}>
                              {item.critic_result.quality_issues.map((issue, i) => (
                                <li key={i}>{String(issue)}</li>
                              ))}
                            </ul>
                          </div>
                        )}
                        {Array.isArray(item.critic_result.all_issues) && item.critic_result.all_issues.length > 0 && (
                          <div>
                            <Typography.Text strong>全部 issue：</Typography.Text>
                            <ul style={{ margin: '4px 0 0 0', paddingLeft: 18 }}>
                              {item.critic_result.all_issues.slice(0, 12).map((issue, i) => (
                                <li key={i}>{String(issue)}</li>
                              ))}
                              {item.critic_result.all_issues.length > 12 && (
                                <li>… 共 {item.critic_result.all_issues.length} 条</li>
                              )}
                            </ul>
                          </div>
                        )}
                        {item.critic_result.leakage_evidence && (
                          <div>
                            <Typography.Text strong>泄题证据：</Typography.Text>
                            <pre style={{ margin: '4px 0 0 0', fontSize: 12, background: '#f5f5f5', padding: 8, borderRadius: 4 }}>
                              {JSON.stringify(item.critic_result.leakage_evidence, null, 2)}
                            </pre>
                          </div>
                        )}
                      </Space>
                    </Card>
                      );
                    })()
                  )}
                  {item.final_json && (() => {
                    const status = getLatestRunStatus(item.steps || []);
                    if (isTaskActive && status.latestRunId > 0 && !status.latestRunHasCriticTerminal && !status.latestRunHasFinalSnapshot) {
                      return null;
                    }
                    const previewMeta = getFinalQuestionPreviewCardMeta(item);
                    return (
                    <Card
                      size="small"
                      title={
                        <Space wrap>
                          <span>{previewMeta.title}</span>
                          <Tag color={previewMeta.tagColor}>{previewMeta.tag}</Tag>
                          <Tooltip title={previewMeta.tooltip}>
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>(?)</Typography.Text>
                          </Tooltip>
                        </Space>
                      }
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
