import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Cascader,
  Checkbox,
  Collapse,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Pagination,
  Row,
  Col,
  Select,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  addBankQuestions,
  cancelGenerateTask,
  createGenerateTask,
  getApiErrorMessage,
  getGenerateTask,
  getSliceImageUrl,
  getSlicePathTree,
  getSlices,
  listGenerateTasks,
  listGenerateTemplates,
  listMaterials,
  resumeGenerateTask,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import MarkdownWithMermaid from '../components/MarkdownWithMermaid';
import QuestionDetailView from '../components/QuestionDetailView';
import { getFinalQuestionPreviewCardMeta } from '../utils/finalQuestionPreviewMeta';
import { getTraceDisplayIndex, getTraceItemKey, mergeTaskTraceForDisplay, sortTraceRows } from '../utils/generateTrace';

/**
 * 教材条目在界面上的展示名：取上传文件名并去掉版本前缀，与任务列表等处一致。
 * @param {{ file_path?: string, material_version_id?: string, status?: string }} m
 * @returns {string}
 */
function materialLabel(m) {
  const raw = String(m?.file_path || '').split('/').pop() || '';
  const name = raw.replace(/^v\d{8}_\d{6}(?:_[a-z0-9]+)?_/, '') || raw || String(m?.material_version_id || '');
  return `${name}${m?.status === 'effective' ? '（生效）' : ''}`;
}

export default function AIGeneratePage() {
  const AUTO_SAVE_PASSED_QUESTIONS = true;
  const DEFAULT_CREATE_FORM_VALUES = {
    task_name: '',
    num_questions: 1,
    question_type: '单选题',
    generation_mode: '随机',
    difficulty: '随机',
  };
  const [createForm] = Form.useForm();
  const [pageMode, setPageMode] = useState('tasks'); // tasks | create
  const navigate = useNavigate();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [taskLoading, setTaskLoading] = useState(false);
  const [rows, setRows] = useState([]);
  const [runTrace, setRunTrace] = useState([]);
  const [selectedGeneratedKeys, setSelectedGeneratedKeys] = useState([]);
  const [savingToBank, setSavingToBank] = useState(false);
  const [savingSingleKeys, setSavingSingleKeys] = useState([]);
  const [cancellingTaskId, setCancellingTaskId] = useState('');
  const [resumingTaskId, setResumingTaskId] = useState('');
  const [errors, setErrors] = useState([]);
  const [stats, setStats] = useState({ generated_count: 0, saved_count: 0 });
  const [viewQuestionOpen, setViewQuestionOpen] = useState(false);
  const [viewQuestionRecord, setViewQuestionRecord] = useState(null);
  const [taskItems, setTaskItems] = useState([]);
  const [taskListLoadError, setTaskListLoadError] = useState('');
  const [taskKeyword, setTaskKeyword] = useState('');
  const [taskStatusFilter, setTaskStatusFilter] = useState('');
  const [taskMaterialFilter, setTaskMaterialFilter] = useState('');
  const taskListRequestInFlightRef = useRef(false);
  const taskDetailRequestInFlightRef = useRef(false);
  const [taskQueryKeyword, setTaskQueryKeyword] = useState('');
  const [taskQueryStatus, setTaskQueryStatus] = useState('');
  const [taskQueryMaterial, setTaskQueryMaterial] = useState('');
  const [activeTaskId, setActiveTaskId] = useState('');
  const activeTaskIdRef = React.useRef(activeTaskId);
  React.useEffect(() => {
    activeTaskIdRef.current = activeTaskId;
  }, [activeTaskId]);
  const [materials, setMaterials] = useState([]);
  /** 全量教材版本（含非生效），用于按 ID 反查真实展示名 */
  const [materialCatalog, setMaterialCatalog] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('');
  const [generateBy, setGenerateBy] = useState('manual'); // manual | template
  const [selectedTemplateId, setSelectedTemplateId] = useState('');
  const [approvedSlices, setApprovedSlices] = useState([]);
  const [materialSliceTotal, setMaterialSliceTotal] = useState(0);
  const [pathTreeOptions, setPathTreeOptions] = useState([]);
  const [selectedPathNodes, setSelectedPathNodes] = useState([]);
  const [sliceKeyword, setSliceKeyword] = useState('');
  const [selectedMastery, setSelectedMastery] = useState([]);
  const [appliedPathNodes, setAppliedPathNodes] = useState([]);
  const [appliedSliceKeyword, setAppliedSliceKeyword] = useState('');
  const [appliedSelectedMastery, setAppliedSelectedMastery] = useState([]);
  const [selectedCalcSliceFilter, setSelectedCalcSliceFilter] = useState(''); // '' | 'yes' | 'no'
  const [appliedCalcSliceFilter, setAppliedCalcSliceFilter] = useState('');
  const [hasQueried, setHasQueried] = useState(false);
  const [selectedSliceKeys, setSelectedSliceKeys] = useState([]);
  const [sliceContentPage, setSliceContentPage] = useState(1);
  const [sliceViewMode, setSliceViewMode] = useState('select');
  const [showSlicePanel, setShowSlicePanel] = useState(false);
  const [autoSelectAllOnNextFilter, setAutoSelectAllOnNextFilter] = useState(false);
  const [genScopeMode, setGenScopeMode] = useState('custom');
  const [traceDetailMode, setTraceDetailMode] = useState('concise');
  const ALL_NODE_PREFIX = '__ALL__::';
  // Concise mode: only these step messages are shown (one line per key event)
  const CONCISE_STEP_MESSAGES = new Set([
    '开始出题',
    '路由完成',
    '初稿题干',
    '题干要点',
    '作家润色完成',
    '题目结果',
    '定稿题干',
    '定稿选项',
    '定稿解析',
    '修复摘要',
    '审核通过',
    '审核驳回',
    '审核动作',
    '审核问题清单',
    '执行修复',
    '题目生成成功',
    '未经过 critic 审核',
    'critic 未通过，题目未保存',
    '未产出 final_json',
    '出题异常',
    '稳定性预警',
  ]);
  const NODE_LABELS = { router: '路由', specialist: '初稿', writer: '作家', critic: '审核', fixer: '修复', calculator: '计算', system: '系统' };
  const CONCISE_DETAIL_MAX_LEN = 80;
  const getVisibleSteps = (steps) => {
    const list = Array.isArray(steps) ? steps : [];
    if (traceDetailMode !== 'concise') return list;
    const filtered = list.filter((s) => CONCISE_STEP_MESSAGES.has(String(s?.message || '').trim()));
    // Never show empty: if filter removed everything (e.g. during early stream), show full list
    return filtered.length > 0 ? filtered : list;
  };
  const splitOptionLines = (detail) => {
    const raw = String(detail || '').trim();
    if (!raw) return [];
    return raw.split(/\s*\|\s*/).map((x) => String(x || '').trim()).filter(Boolean);
  };
  const getRunIds = (steps) => {
    const list = Array.isArray(steps) ? steps : [];
    const runIds = [];
    let inferredRun = 0;
    let routerSeen = false;
    list.forEach((step) => {
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
  const getLatestRunStatus = (steps) => {
    const list = Array.isArray(steps) ? steps : [];
    const runIds = getRunIds(list);
    let latestRunId = 0;
    runIds.forEach((rid) => {
      if (Number.isFinite(rid) && rid > latestRunId) latestRunId = rid;
    });
    let latestRunHasCriticTerminal = false;
    let latestRunHasWriterDone = false;
    let latestRunHasFinalSnapshot = false;
    list.forEach((step, idx) => {
      const rid = runIds[idx];
      if (rid !== latestRunId) return;
      const node = String(step?.node || '');
      const msg = String(step?.message || '');
      const detail = String(step?.detail || '');
      if (node === 'critic' && (msg === '审核通过' || msg === '审核驳回')) {
        latestRunHasCriticTerminal = true;
      }
      if (node === 'writer' && (msg === '作家润色完成' || detail.includes('进入 critic'))) {
        latestRunHasWriterDone = true;
      }
      if ((node === 'writer' || node === 'fixer') && (
        msg === '作家润色完成'
        || msg.includes('定稿题干')
        || msg.includes('定稿选项')
        || msg.includes('定稿解析')
        || msg === '题目结果'
      )) {
        latestRunHasFinalSnapshot = true;
      }
    });
    return {
      latestRunId,
      latestRunHasCriticTerminal,
      latestRunHasWriterDone,
      latestRunHasFinalSnapshot,
    };
  };
  const stripOptionPrefix = (line) => String(line || '').replace(/^\s*[A-Ha-h][\.\、\s]+/, '').trim();
  const normalizeOptionLine = (line, idx) => {
    const cleaned = stripOptionPrefix(line);
    const optionKey = String.fromCharCode(65 + idx);
    return `${optionKey}. ${cleaned || String(line || '').trim()}`;
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
  const truncateDetail = (text, maxLen) => {
    const s = String(text || '').trim();
    if (!s || maxLen <= 0) return s;
    const firstLine = s.split(/\r?\n/)[0] || '';
    if (firstLine.length <= maxLen) return firstLine;
    return `${firstLine.slice(0, maxLen)}…`;
  };
  const renderNormalStep = (item, step, idx) => {
    const concise = traceDetailMode === 'concise';
    const detail = step.detail
      ? (concise ? truncateDetail(step.detail, CONCISE_DETAIL_MAX_LEN) : step.detail)
      : '';
    const nodeColor = (
      step.level === 'success'
        ? 'green'
        : step.level === 'error'
          ? 'red'
          : step.level === 'warning'
            ? 'orange'
            : 'blue'
    );
    return (
      <Space key={`${getTraceItemKey(item, idx)}_${step.seq || idx}`} size={8} wrap>
        <Tag color={nodeColor}>{step.node || 'system'}</Tag>
        <Typography.Text>{step.message}</Typography.Text>
        {detail ? (
          <Typography.Text type="secondary" style={{ whiteSpace: 'pre-wrap' }}>
            {detail}
          </Typography.Text>
        ) : null}
      </Space>
    );
  };
  const renderQuestionGroup = (item, group, idx) => {
    const concise = traceDetailMode === 'concise';
    const nodeLabel = NODE_LABELS[group.node] || group.node || '';
    const phaseTitle = nodeLabel ? `${nodeLabel}（${group.phase}）` : group.phase;
    if (concise) {
      const stemShort = group.stem ? truncateDetail(group.stem, 50) : '';
      const result = group.result || '';
      return (
        <div key={`${getTraceItemKey(item, idx)}_qg_${group.node}_${idx}`} style={{ marginBottom: 4 }}>
          <Typography.Text type="secondary">
            {phaseTitle}：{stemShort}
            {result ? ` | ${result}` : ''}
          </Typography.Text>
        </div>
      );
    }
    const optionLines = splitOptionLines(group.options || '').map((line, i) => normalizeOptionLine(line, i));
    return (
      <div
        key={`${getTraceItemKey(item, idx)}_qg_${group.node}_${idx}`}
        style={{
          border: '1px solid #e5e6eb',
          borderRadius: 8,
          padding: '10px 12px',
          background: '#fafcff',
        }}
      >
        <Typography.Text strong>{phaseTitle}</Typography.Text>
        {group.stem ? (
          <Typography.Paragraph style={{ margin: '8px 0 0 0', whiteSpace: 'pre-wrap' }}>
            {group.stem}
          </Typography.Paragraph>
        ) : null}
        {optionLines.length > 0 ? (
          <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 6 }}>
            {optionLines.map((line, i) => (
              <Typography.Text key={`${getTraceItemKey(item, idx)}_qg_${group.node}_opt_${i}`}>{line}</Typography.Text>
            ))}
          </Space>
        ) : null}
        {group.explanation ? (
          <Typography.Paragraph style={{ margin: '8px 0 0 0', whiteSpace: 'pre-wrap' }}>
            {group.explanation}
          </Typography.Paragraph>
        ) : null}
        {group.result ? (
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
            {group.result}
          </Typography.Text>
        ) : null}
      </div>
    );
  };
  const renderTraceEntries = (item) => {
    const rawSteps = Array.isArray(item?.steps) ? item.steps : [];
    const rawRunIds = getRunIds(rawSteps);
    const status = getLatestRunStatus(rawSteps);
    const latestRunOnly = Boolean(loading && status.latestRunId > 0 && !status.latestRunHasCriticTerminal);
    const rawIndexed = rawSteps.map((step, idx) => ({ step, runId: rawRunIds[idx], rawIdx: idx }));
    const scoped = latestRunOnly
      ? rawIndexed.filter((entry) => entry.runId === status.latestRunId)
      : rawIndexed;
    const visibleEntries = getVisibleSteps(scoped.map((entry) => entry.step));
    const visibleSet = new Set(visibleEntries);
    const entries = scoped.filter((entry) => visibleSet.has(entry.step));
    const steps = entries.map((entry) => entry.step);
    const runIds = entries.map((entry) => entry.runId);
    const groupByKey = new Map();
    const firstQuestionIdxByKey = new Map();
    steps.forEach((step, idx) => {
      const parsed = parseQuestionStep(step);
      if (!parsed) return;
      const node = step?.node || 'system';
      const runId = runIds[idx];
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
    const orderedRows = [];
    steps.forEach((step, idx) => {
      const parsed = parseQuestionStep(step);
      const node = step?.node || 'system';
      const runId = runIds[idx];
      const key = `${node}_${runId}`;
      if (!parsed) {
        orderedRows.push({ type: 'normal', step, idx });
        return;
      }
      const firstIdx = firstQuestionIdxByKey.get(key);
      if (firstIdx !== idx) {
        return;
      }
      const g = groupByKey.get(key);
      if (!g || (!g.stem && !g.options && !g.explanation && !g.result)) return;
      orderedRows.push({
        type: 'question_group',
        group: {
          node: g.node,
          phase: g.stemRank >= 2 ? '定稿' : g.stemRank >= 1 ? '初稿' : '题目',
          stem: g.stem,
          options: g.options,
          explanation: g.explanation,
          result: g.result,
        },
      });
    });
    return orderedRows.map((row, i) =>
      row.type === 'normal'
        ? renderNormalStep(item, row.step, row.idx)
        : renderQuestionGroup(item, row.group, i)
    );
  };

  /**
   * 单题折叠区：步骤流水 + writer/fixer 当前完整定稿（含未通过 critic 的最后一版，便于排查）
   * @param {Record<string, unknown>} item
   */
  const renderTracePanelBody = (item) => (
    <Space direction="vertical" style={{ width: '100%' }} size={4}>
      {(() => {
        const status = getLatestRunStatus(item?.steps || []);
        if (!loading || status.latestRunId <= 0 || status.latestRunHasCriticTerminal) return null;
        return (
          <Alert
            type="info"
            showIcon
            message={`当前题已进入第 ${status.latestRunId + 1} 轮重试`}
            description="为避免把上一轮 writer/fixer 定稿和当前轮 specialist/calculator 初稿混在一起，当前只展示最新轮次。"
          />
        );
      })()}
      <Typography.Text type="secondary">{item.slice_path || '（无路径）'}</Typography.Text>
      {traceDetailMode === 'full' ? (
        <div style={{ maxHeight: 320, overflow: 'auto' }}>
          <MarkdownWithMermaid text={buildTraceSliceMarkdown(item)} />
        </div>
      ) : (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          （过程详细时可查看切片内容）
        </Typography.Text>
      )}
      {renderTraceEntries(item)}
      {(() => {
        const fj = item?.final_json;
        if (!fj || typeof fj !== 'object' || Array.isArray(fj)) return null;
        const status = getLatestRunStatus(item?.steps || []);
        if (loading && status.latestRunId > 0 && !status.latestRunHasCriticTerminal && !status.latestRunHasFinalSnapshot) {
          return null;
        }
        const previewMeta = getFinalQuestionPreviewCardMeta(item);
        return (
          <Card
            size="small"
            style={{ marginTop: 8 }}
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
            <QuestionDetailView question={fj} />
          </Card>
        );
      })()}
    </Space>
  );

  const approvedSliceById = useMemo(() => {
    const m = new Map();
    (approvedSlices || []).forEach((row) => {
      const sid = Number(row?.slice_id || 0);
      if (!sid) return;
      m.set(sid, row);
    });
    return m;
  }, [approvedSlices]);
  const orderedRunTrace = useMemo(() => sortTraceRows(runTrace), [runTrace]);
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
        const url = getSliceImageUrl(tenantId, p, row?.material_version_id || materialVersionId);
        return `- ${title}\n\n  ![${title}](${url})`;
      })
      .filter(Boolean);
    if (!imageLines.length) return content || '（无切片内容）';
    return `${content || ''}\n\n---\n\n### 切片图片\n${imageLines.join('\n\n')}`;
  };
  const buildTemplateTaskName = (template) => {
    const base = String(template?.name || '模板出题').trim() || '模板出题';
    const now = new Date();
    const yyyy = now.getFullYear();
    const mm = String(now.getMonth() + 1).padStart(2, '0');
    const dd = String(now.getDate()).padStart(2, '0');
    const hh = String(now.getHours()).padStart(2, '0');
    const mi = String(now.getMinutes()).padStart(2, '0');
    return `${base}-${yyyy}${mm}${dd}-${hh}${mi}`;
  };
  const formatTime = (value) => {
    const s = String(value || '').trim();
    if (!s) return '-';
    return s.replace('T', ' ').replace(/\.\d+\+\d{2}:\d{2}$/, '');
  };
  const isTaskRunning = (status) => ['pending', 'running'].includes(String(status || ''));
  // When expectedTaskId is given, only apply if it still matches current active task (avoids overwriting with stale task after new task creation)
  // Normalize legacy task-timeout errors (task-level timeout has been removed)
  const normalizeTaskErrors = (rawErrors) => {
    const list = Array.isArray(rawErrors) ? rawErrors : [];
    return list.map((e) => {
      const s = String(e || '').trim();
      if (/任务执行超时|task\s*execution\s*timeout/i.test(s)) {
        return '任务执行失败（当前版本已取消任务执行时间限制；该错误可能来自历史任务记录）';
      }
      return s;
    });
  };
  const applyTaskDetail = (task, expectedTaskId) => {
    const t = task && typeof task === 'object' ? task : {};
    const taskId = String(t.task_id || '');
    if (expectedTaskId != null && expectedTaskId !== '' && (taskId !== String(expectedTaskId) || activeTaskIdRef.current !== String(expectedTaskId))) {
      return;
    }
    setLoading(isTaskRunning(t.status));
    const items = Array.isArray(t.items) ? t.items : [];
    setRows(items.map((item, idx) => ({ ...item, _gen_key: `${String(t.task_id || 'task')}_${idx}` })));
    setRunTrace(mergeTaskTraceForDisplay(t));
    setErrors(normalizeTaskErrors(t.errors));
    setStats({
      generated_count: Number(t.generated_count || 0),
      saved_count: Number(t.saved_count || 0),
    });
  };
  // skipSetActiveWhenEmpty: when true, only refresh taskItems; do not set activeTaskId when it is '' (used in create-mode "new task" flow to avoid showing history)
  const loadTaskList = async (tid, keepActive = true, skipSetActiveWhenEmpty = false) => {
    if (!tid) return [];
    if (taskListRequestInFlightRef.current) return taskItems;
    taskListRequestInFlightRef.current = true;
    try {
      const res = await listGenerateTasks(tid, { limit: 100 });
      const items = Array.isArray(res?.items) ? res.items : [];
      setTaskItems(items);
      setTaskListLoadError('');
      if (!items.length) {
        if (!keepActive && !skipSetActiveWhenEmpty) setActiveTaskId('');
        return [];
      }
      if (!skipSetActiveWhenEmpty && (!keepActive || !activeTaskId)) {
        setActiveTaskId(String(items[0].task_id || ''));
        return items;
      }
      const hasActive = items.some((x) => String(x?.task_id || '') === String(activeTaskId));
      if (!skipSetActiveWhenEmpty && !hasActive) setActiveTaskId(String(items[0].task_id || ''));
      return items;
    } catch (e) {
      const msg = getApiErrorMessage(e, '加载任务列表失败');
      setTaskListLoadError(msg);
      return taskItems;
    } finally {
      taskListRequestInFlightRef.current = false;
    }
  };

  const filteredTaskItems = useMemo(() => {
    return (taskItems || []).filter((t) => {
      const status = String(t?.status || '');
      const material = String(t?.material_version_id || '');
      const taskName = String(t?.task_name || '');
      if (taskQueryStatus && status !== taskQueryStatus) return false;
      if (taskQueryMaterial && material !== taskQueryMaterial) return false;
      if (taskQueryKeyword) {
        const hay = taskName.toLowerCase();
        if (!hay.includes(taskQueryKeyword.toLowerCase())) return false;
      }
      return true;
    });
  }, [taskItems, taskQueryKeyword, taskQueryMaterial, taskQueryStatus]);
  const selectedTemplate = useMemo(
    () => (templates || []).find((item) => String(item?.template_id || '') === String(selectedTemplateId || '')) || null,
    [templates, selectedTemplateId],
  );

  /** 当前模板绑定教材的展示名（非裸 version id） */
  const selectedTemplateMaterialDisplayName = useMemo(() => {
    const mid = String(selectedTemplate?.material_version_id || '').trim();
    if (!mid) return '';
    const target = (materialCatalog || []).find((m) => String(m?.material_version_id || '') === mid);
    return target ? materialLabel(target) : mid;
  }, [materialCatalog, selectedTemplate]);

  const templateSelectOptions = useMemo(
    () =>
      (templates || []).map((item) => {
        const mid = String(item?.material_version_id || '');
        const target = (materialCatalog || []).find((m) => String(m?.material_version_id || '') === mid);
        const matPart = target ? materialLabel(target) : mid;
        return {
          label: `${item.name}｜${matPart}｜${item.question_count}题`,
          value: item.template_id,
        };
      }),
    [templates, materialCatalog],
  );

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    if (!tenantId) return;
    setPageMode('tasks');
    setHasQueried(false);
    setSelectedSliceKeys([]);
    setRows([]);
    setRunTrace([]);
    setTaskListLoadError('');
    setActiveTaskId('');
    setTemplates([]);
    setGenerateBy('manual');
    setSelectedTemplateId('');
    setSelectedGeneratedKeys([]);
    setErrors([]);
    setStats({ generated_count: 0, saved_count: 0 });
    setShowSlicePanel(false);
    setAutoSelectAllOnNextFilter(false);
    loadMaterials(tenantId);
    loadTemplates(tenantId);
    loadTaskList(tenantId, false).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  useEffect(() => {
    if (pageMode !== 'create') return;
    createForm.setFieldsValue(DEFAULT_CREATE_FORM_VALUES);
  }, [createForm, pageMode]);

  useEffect(() => {
    if (!tenantId || !activeTaskId) return undefined;
    if (pageMode !== 'create') return undefined;
    const pollingTaskId = activeTaskId;
    let cancelled = false;
    const tick = async () => {
      if (taskDetailRequestInFlightRef.current) {
        if (!cancelled) setTimeout(tick, 2500);
        return;
      }
      taskDetailRequestInFlightRef.current = true;
      try {
        const res = await getGenerateTask(tenantId, pollingTaskId);
        if (cancelled) return;
        const task = res?.task || {};
        applyTaskDetail(task, pollingTaskId);
        if (isTaskRunning(task?.status)) {
          setTimeout(tick, 2500);
        } else {
          loadTaskList(tenantId, true).catch(() => {});
        }
      } catch (_e) {
        if (!cancelled) setTimeout(tick, 3000);
      } finally {
        taskDetailRequestInFlightRef.current = false;
      }
    };
    tick();
    return () => {
      cancelled = true;
      taskDetailRequestInFlightRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, activeTaskId, pageMode]);

  useEffect(() => {
    if (!tenantId) return undefined;
    const timer = setInterval(() => {
      // In create mode with no active task (new-task flow), only refresh list; do not set activeTaskId to avoid showing previous task's result
      const skipSetActive = pageMode === 'create' && !activeTaskId;
      loadTaskList(tenantId, true, skipSetActive).catch(() => {});
    }, 10000);
    return () => clearInterval(timer);
  }, [tenantId, pageMode, activeTaskId]);

  const loadPathTree = async (tid, mid) => {
    if (!tid) return;
    try {
      const res = await getSlicePathTree(tid, {
        status: 'approved',
        material_version_id: mid || undefined,
      });
      setPathTreeOptions(res.options || []);
    } catch (e) {
      setPathTreeOptions([]);
    }
  };

  const loadApprovedSlices = async (tid, mid) => {
    if (!tid) return;
    try {
      let page = 1;
      const all = [];
      while (true) {
        // backend page_size max is 200
        // eslint-disable-next-line no-await-in-loop
        const res = await getSlices(tid, { status: 'approved', material_version_id: mid || undefined, page, page_size: 200 });
        const items = res.items || [];
        all.push(...items);
        if (all.length >= (res.total || 0) || items.length === 0) break;
        page += 1;
      }
      setApprovedSlices(all);
    } catch (e) {
      message.error(getApiErrorMessage(e, '加载已审核切片失败'));
    }
  };

  const loadMaterialSliceTotal = async (tid, mid) => {
    if (!tid) return;
    try {
      const res = await getSlices(tid, {
        status: 'all',
        material_version_id: mid || undefined,
        page: 1,
        page_size: 1,
      });
      setMaterialSliceTotal(res.total || 0);
    } catch (e) {
      setMaterialSliceTotal(0);
    }
  };

  const loadMaterials = async (tid) => {
    if (!tid) return;
    try {
      const res = await listMaterials(tid);
      const items = res.items || [];
      setMaterialCatalog(Array.isArray(items) ? items : []);
      const effectiveItems = items.filter((x) => String(x?.status || '') === 'effective');
      setMaterials(effectiveItems);
      const chosen = (effectiveItems[0] || {}).material_version_id || '';
      setMaterialVersionId(chosen);
      if (!chosen) {
        setApprovedSlices([]);
        setMaterialSliceTotal(0);
        setPathTreeOptions([]);
        return;
      }
      await loadApprovedSlices(tid, chosen);
      await loadMaterialSliceTotal(tid, chosen);
      await loadPathTree(tid, chosen);
    } catch (e) {
      setMaterials([]);
      setMaterialCatalog([]);
      setMaterialVersionId('');
      setApprovedSlices([]);
      setMaterialSliceTotal(0);
      setPathTreeOptions([]);
      message.error(getApiErrorMessage(e, '加载教材版本失败'));
    }
  };

  const loadTemplates = async (tid) => {
    if (!tid) return;
    try {
      const res = await listGenerateTemplates(tid);
      setTemplates(res?.items || []);
    } catch (e) {
      setTemplates([]);
      message.error(e?.response?.data?.error?.message || '加载出题模板失败');
    }
  };

  const withAllOption = (options, parentPath = []) => {
    if (!Array.isArray(options) || options.length === 0) return [];
    const mapped = options.map((opt) => {
      const currentPath = [...parentPath, String(opt.value)];
      const item = { ...opt };
      if (Array.isArray(item.children) && item.children.length) {
        item.children = withAllOption(item.children, currentPath);
      }
      return item;
    });
    const allValue = `${ALL_NODE_PREFIX}${parentPath.join(' > ')}`;
    return [{ label: '全选', value: allValue }, ...mapped];
  };

  const toPathPrefix = (vals) => {
    if (!Array.isArray(vals) || vals.length === 0) return '';
    const allNode = vals.find((v) => String(v).startsWith(ALL_NODE_PREFIX));
    if (allNode) return String(allNode).slice(ALL_NODE_PREFIX.length);
    return vals.map((v) => String(v)).join(' > ');
  };

  const onSubmit = async (values) => {
    if (!tenantId) return;
    setTaskLoading(true);
    setLoading(true);
    setRows([]);
    setRunTrace([]);
    setSelectedGeneratedKeys([]);
    setErrors([]);
    setStats({ generated_count: 0, saved_count: 0 });
    try {
      const taskName = String(values.task_name || '').trim();
      if (!taskName) {
        message.warning('请输入任务名称');
        setLoading(false);
        return;
      }
      const duplicatedName = (taskItems || []).some(
        (x) => String(x?.task_name || '').trim().toLowerCase() === taskName.toLowerCase(),
      );
      if (duplicatedName) {
        message.warning('任务名称已存在，请使用不同名称');
        setLoading(false);
        return;
      }
      const candidateIds = selectedSliceKeys.map((x) => Number(x)).filter((x) => Number.isFinite(x));
      if (generateBy === 'manual' && !candidateIds.length) {
        message.warning('请先在左侧勾选至少一个知识切片');
        setLoading(false);
        return;
      }
      if (generateBy === 'template' && !selectedTemplate) {
        message.warning('请选择出题模板');
        setLoading(false);
        return;
      }
      const templateMaterialVersionId = String(selectedTemplate?.material_version_id || '');
      const payload = {
        task_name: taskName,
        gen_scope_mode: genScopeMode,
        num_questions: generateBy === 'template'
          ? Number(selectedTemplate?.question_count || 1)
          : (genScopeMode === 'per_slice' ? candidateIds.length : (values.num_questions || 1)),
        question_type: values.question_type || '单选题',
        generation_mode: values.generation_mode || '随机',
        difficulty: values.difficulty || '随机',
        enable_offline_judge: false,
        save_to_bank: AUTO_SAVE_PASSED_QUESTIONS,
        slice_ids: generateBy === 'template' ? [] : candidateIds,
        material_version_id: generateBy === 'template'
          ? (templateMaterialVersionId || undefined)
          : (materialVersionId || undefined),
        template_id: generateBy === 'template' ? String(selectedTemplate?.template_id || '') : undefined,
        template_name: generateBy === 'template' ? String(selectedTemplate?.name || '') : undefined,
      };
      const res = await createGenerateTask(tenantId, payload);
      const task = res?.task || {};
      const taskId = String(task?.task_id || '');
      if (!taskId) throw new Error('任务创建失败');
      setActiveTaskId(taskId);
      await loadTaskList(tenantId, true);
      message.success(`任务已创建：${taskId}，可切换页面继续执行`);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || e?.message || '出题失败');
      setLoading(false);
    } finally {
      setTaskLoading(false);
    }
  };

  const onTaskQuery = () => {
    setTaskQueryKeyword(taskKeyword.trim());
    setTaskQueryStatus(taskStatusFilter);
    setTaskQueryMaterial(taskMaterialFilter);
  };

  const onTaskReset = () => {
    setTaskKeyword('');
    setTaskStatusFilter('');
    setTaskMaterialFilter('');
    setTaskQueryKeyword('');
    setTaskQueryStatus('');
    setTaskQueryMaterial('');
  };

  const onSaveSelectedToBank = async () => {
    if (!tenantId) return;
    if (!selectedGeneratedKeys.length) {
      message.warning('请先勾选要入库的题目');
      return;
    }
    const selectedSet = new Set(selectedGeneratedKeys.map((x) => String(x)));
    const selectedItems = rows.filter((x) => selectedSet.has(String(x._gen_key)));
    if (!selectedItems.length) {
      message.warning('未找到可入库题目');
      return;
    }
    setSavingToBank(true);
    try {
      const res = await addBankQuestions(tenantId, {
        items: selectedItems,
        material_version_id: materialVersionId || undefined,
      });
      const added = res?.added || 0;
      setStats((s) => ({ ...s, saved_count: (s.saved_count || 0) + added }));
      setSelectedGeneratedKeys([]);
      message.success(`已手动入库 ${added} 题`);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '入库失败');
    } finally {
      setSavingToBank(false);
    }
  };

  const onSaveOneToBank = async (record) => {
    if (!tenantId || !record) return;
    const rowKey = String(record._gen_key || '');
    if (!rowKey) return;
    setSavingSingleKeys((prev) => [...prev, rowKey]);
    try {
      const res = await addBankQuestions(tenantId, {
        items: [record],
        material_version_id: materialVersionId || undefined,
      });
      const added = res?.added || 0;
      setStats((s) => ({ ...s, saved_count: (s.saved_count || 0) + added }));
      message.success(added > 0 ? '该题已加入题库' : '该题未加入题库（可能重复）');
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '单题入库失败');
    } finally {
      setSavingSingleKeys((prev) => prev.filter((k) => k !== rowKey));
    }
  };

  const applySliceFilters = () => {
    setAppliedPathNodes(selectedPathNodes || []);
    setAppliedSliceKeyword(sliceKeyword || '');
    setAppliedSelectedMastery(selectedMastery || []);
    setAppliedCalcSliceFilter(selectedCalcSliceFilter || '');
  };

  const onQueryFilters = async () => {
    if (!tenantId) return;
    await loadApprovedSlices(tenantId, materialVersionId);
    await loadPathTree(tenantId, materialVersionId);
    applySliceFilters();
    setHasQueried(true);
    setAutoSelectAllOnNextFilter(true);
    setSliceContentPage(1);
    setShowSlicePanel(false);
  };

  const pathPrefix = toPathPrefix(appliedPathNodes);
  const chapterFiltered = approvedSlices.filter((s) => {
    const p = String(s.path || '');
    const content = String(s.slice_content || s.preview || '');
    if (pathPrefix && !p.startsWith(pathPrefix)) return false;
    if (appliedSliceKeyword && !(`${p}\n${content}`.includes(appliedSliceKeyword))) return false;
    return true;
  });
  const masteryOptions = Array.from(new Set(chapterFiltered.map((s) => s.mastery).filter(Boolean)));
  const ALL_MASTERY_VALUE = '__ALL_MASTERY__';
  const selectedMasteryForFilter = appliedSelectedMastery.includes(ALL_MASTERY_VALUE)
    ? []
    : appliedSelectedMastery;
  const calcSliceFilter = appliedCalcSliceFilter || '';
  const finalSlices = chapterFiltered
    .filter((s) => (!selectedMasteryForFilter.length ? true : selectedMasteryForFilter.includes(s.mastery)))
    .filter((s) => {
      const isCalc = Boolean(s && s.is_calculation_slice);
      if (!calcSliceFilter) return true;
      return calcSliceFilter === 'yes' ? isCalc : !isCalc;
    });
  const hasAnyCalcSliceInApproved = (approvedSlices || []).some((s) => Boolean(s && s.is_calculation_slice));
  const contentPageSize = 6;
  const pagedContentSlices = finalSlices.slice(
    Math.max(0, (sliceContentPage - 1) * contentPageSize),
    Math.max(0, sliceContentPage * contentPageSize)
  );
  useEffect(() => {
    if (!hasQueried) return;
    if (autoSelectAllOnNextFilter) {
      setSelectedSliceKeys(finalSlices.map((s) => String(s.slice_id)));
      setAutoSelectAllOnNextFilter(false);
      return;
    }
    const validKeys = new Set(finalSlices.map((s) => String(s.slice_id)));
    setSelectedSliceKeys((prev) => prev.filter((k) => validKeys.has(String(k))));
  }, [hasQueried, finalSlices, autoSelectAllOnNextFilter]);
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(finalSlices.length / contentPageSize));
    if (sliceContentPage > maxPage) setSliceContentPage(1);
  }, [finalSlices.length, sliceContentPage]);

  const zeroReason = (() => {
    if (!materialVersionId) return '请先选择教材版本。';
    if (materialSliceTotal === 0) return '当前教材还没有生成切片，请先到「资源上传」上传教材并生成切片。';
    if (approvedSlices.length === 0) return '当前教材已有切片，但还没有审核通过（approved）的切片，请先到「切片核对」完成审核。';
    if (chapterFiltered.length === 0) return '当前路径/关键词筛选后没有命中切片，请放宽筛选条件后再试。';
    if (finalSlices.length === 0) {
      if (appliedCalcSliceFilter === 'yes' && !hasAnyCalcSliceInApproved) {
        return '当前返回的切片中没有任何一条被标记为计算题。请先选「全部」确认有可出题切片；若后端刚更新过，请重启后端后再试。';
      }
      return '当前掌握程度或计算题切片筛选后没有可出题切片，请调整筛选条件。';
    }
    return '';
  })();

  const columns = [
    { title: '题干', dataIndex: '题干', ellipsis: true },
    { title: '答案', dataIndex: '正确答案', width: 100, render: (v) => <Tag color="green">{v}</Tag> },
    { title: '难度值', dataIndex: '难度值', width: 100 },
    { title: '来源切片', dataIndex: '来源路径', ellipsis: true },
    {
      title: '查看',
      dataIndex: '_view',
      width: 90,
      render: (_, record) => (
        <Button
          size="small"
          onClick={() => {
            setViewQuestionRecord(record);
            setViewQuestionOpen(true);
          }}
        >
          查看
        </Button>
      ),
    },
    ...(
      AUTO_SAVE_PASSED_QUESTIONS
        ? []
        : [{
          title: '操作',
          dataIndex: '_action',
          width: 120,
          render: (_, record) => (
            <Button
              size="small"
              onClick={() => onSaveOneToBank(record)}
              loading={savingSingleKeys.includes(String(record._gen_key || ''))}
            >
              加入题库
            </Button>
          ),
        }]
    ),
  ];
  const taskColumns = [
    {
      title: '出题任务名',
      dataIndex: 'task_name',
      width: 180,
      ellipsis: true,
      render: (v) => String(v || '-') || '-',
    },
    {
      title: '教材',
      dataIndex: 'material_version_id',
      width: 220,
      ellipsis: true,
      render: (v) => {
        const mid = String(v || '');
        const target = materials.find((m) => String(m?.material_version_id || '') === mid);
        if (!target) return mid || '-';
        return materialLabel(target);
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (v) => {
        const status = String(v || '');
        const color = status === 'completed'
          ? 'green'
          : status === 'failed'
            ? 'red'
            : status === 'cancelled'
              ? 'default'
              : status === 'running'
                ? 'blue'
                : 'gold';
        return <Tag color={color}>{status || '-'}</Tag>;
      },
    },
    {
      title: '任务创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v) => formatTime(v),
    },
    {
      title: '任务完成时间',
      dataIndex: 'ended_at',
      width: 180,
      render: (v) => formatTime(v),
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 120,
      render: (v) => `${Number(v?.current || 0)}/${Number(v?.total || 0)}`,
    },
    {
      title: '结果',
      width: 140,
      render: (_, r) => `${Number(r?.generated_count || 0)} / 入库 ${Number(r?.saved_count || 0)}`,
    },
    {
      title: '操作',
      width: 200,
      render: (_, r) => (
        <Space size={8}>
          <Button
            size="small"
            onClick={() => navigate(`/ai-generate/tasks/${encodeURIComponent(String(r?.task_id || ''))}`)}
          >
            出题过程
          </Button>
          {(() => {
            const status = String(r?.status || '').trim();
            const reqTotal = Number(r?.request?.num_questions || 0);
            const progressTotal = Number(r?.progress?.total || 0);
            const targetTotal = Math.max(reqTotal, progressTotal);
            const generated = Number(r?.generated_count || 0);
            const hasRemaining = targetTotal > 0 && generated < targetTotal;
            const canResume = (status === 'failed') || (!['pending', 'running'].includes(status) && hasRemaining);
            if (!canResume) return null;
            return (
              <Button
                size="small"
                type="primary"
                loading={resumingTaskId === String(r?.task_id || '')}
                disabled={!!resumingTaskId && resumingTaskId !== String(r?.task_id || '')}
                onClick={async () => {
                  const taskId = String(r?.task_id || '').trim();
                  if (!tenantId || !taskId) return;
                  setResumingTaskId(taskId);
                  try {
                    await resumeGenerateTask(tenantId, taskId, {});
                    message.success('已开始继续出题');
                    await loadTaskList(tenantId, true);
                  } catch (e) {
                    message.error(e?.response?.data?.error?.message || e?.message || '继续出题失败');
                  } finally {
                    setResumingTaskId('');
                  }
                }}
              >
                继续出题
              </Button>
            );
          })()}
        </Space>
      ),
    },
  ];
  const slicePreviewColumns = [
    { title: '切片ID', dataIndex: 'slice_id', width: 110 },
    { title: '来源路径', dataIndex: 'path', ellipsis: true },
    {
      title: '切片内容',
      dataIndex: 'slice_content',
      ellipsis: true,
      render: (_, record) => String(record.slice_content || record.preview || ''),
    },
    { title: '掌握程度', dataIndex: 'mastery', width: 120 },
  ];
  const hasGenerationSession = loading
    || runTrace.length > 0
    || rows.length > 0
    || errors.length > 0
    || (stats.generated_count || 0) > 0
    || (stats.saved_count || 0) > 0;
  // In create mode, only show result area when there is an active run (avoid showing previous run as "history")
  const showResultInCreateMode = hasGenerationSession && (loading || !!activeTaskId);
  return (
    <>
      {pageMode === 'tasks' && (
        <>
          {taskListLoadError ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message={`任务列表加载失败：${taskListLoadError}`}
              description="已保留当前页面已有任务；可点击“刷新列表”重试。若持续失败，请检查系统号/OIDC Token 或后端服务状态。"
            />
          ) : null}
          <Card style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <Space wrap>
                <Input
                  value={taskKeyword}
                  onChange={(e) => setTaskKeyword(e.target.value)}
                  placeholder="任务名称"
                  style={{ width: 260 }}
                />
                <Select
                  value={taskStatusFilter || undefined}
                  allowClear
                  placeholder="任务状态"
                  style={{ width: 160 }}
                  onChange={(v) => setTaskStatusFilter(v || '')}
                  options={[
                    { label: 'pending', value: 'pending' },
                    { label: 'running', value: 'running' },
                    { label: 'completed', value: 'completed' },
                    { label: 'failed', value: 'failed' },
                    { label: 'cancelled', value: 'cancelled' },
                  ]}
                />
                <Select
                  value={taskMaterialFilter || undefined}
                  allowClear
                  placeholder="教材版本"
                  style={{ width: 260 }}
                  onChange={(v) => setTaskMaterialFilter(v || '')}
                  options={materials.map((m) => ({ label: materialLabel(m), value: m.material_version_id }))}
                />
                <Button type="primary" onClick={onTaskQuery}>查询</Button>
                <Button onClick={onTaskReset}>重置</Button>
              </Space>
              <Space wrap>
                <Button onClick={() => loadTaskList(tenantId, true)}>刷新列表</Button>
                <Button
                  type="primary"
                  onClick={() => {
                    // Reset generation session when starting a brand new task
                    setPageMode('create');
                    setActiveTaskId('');
                    setRows([]);
                    setRunTrace([]);
                    setSelectedGeneratedKeys([]);
                    setErrors([]);
                    setStats({ generated_count: 0, saved_count: 0 });
                    setGenerateBy('manual');
                    setSelectedTemplateId('');
                  }}
                >
                  新建出题任务
                </Button>
              </Space>
            </div>
          </Card>

          <Card style={{ marginBottom: 12 }}>
            {!filteredTaskItems.length && taskItems.length > 0 ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="当前筛选条件下没有任务"
                description="请点击“重置”清空任务名称、状态和教材版本筛选。"
              />
            ) : null}
            <Table
              rowKey={(record) => String(record.task_id || '')}
              size="small"
              columns={taskColumns}
              dataSource={filteredTaskItems}
              pagination={{ pageSize: 8, showSizeChanger: false }}
            />
          </Card>

        </>
      )}

      {pageMode === 'create' && (
      <Card style={{ marginBottom: 12 }}>
        <Alert
          type="info"
          showIcon
          message={
            AUTO_SAVE_PASSED_QUESTIONS
              ? '只会用当前城市、当前教材中“已通过”的切片出题。题目生成后将自动入库（仅 critic 审核通过的题）。'
              : '只会用当前城市、当前教材中“已通过”的切片出题。题目生成后，请手动勾选再点击“入库所选”。'
          }
          style={{ marginBottom: 12 }}
        />
        <Space wrap style={{ width: '100%', marginBottom: 12, justifyContent: 'space-between' }}>
          <Space wrap>
            <Typography.Text style={{ color: 'rgba(0, 0, 0, 0.88)', fontWeight: 500 }}>出题方式：</Typography.Text>
            <Segmented
              value={generateBy}
              onChange={(value) => {
                const next = String(value || 'manual');
                setGenerateBy(next);
                if (next === 'template') {
                  createForm.setFieldValue('question_type', '随机');
                }
              }}
              options={[
                { label: '手动选切片', value: 'manual' },
                { label: '按模板出题', value: 'template' },
              ]}
            />
          </Space>
          <Button onClick={() => navigate('/generate-templates')}>去配置模板</Button>
        </Space>
        {generateBy === 'template' ? (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <Select
              value={selectedTemplateId || undefined}
              placeholder="选择出题模板"
              style={{ width: 420 }}
              onChange={(value) => {
                const nextId = String(value || '');
                setSelectedTemplateId(nextId);
                const target = (templates || []).find((item) => String(item?.template_id || '') === nextId);
                if (!target) return;
                setMaterialVersionId(String(target.material_version_id || ''));
                createForm.setFieldValue('num_questions', Number(target.question_count || 1));
                const currentTaskName = String(createForm.getFieldValue('task_name') || '').trim();
                if (!currentTaskName) {
                  createForm.setFieldValue('task_name', buildTemplateTaskName(target));
                }
              }}
              options={templateSelectOptions}
            />
            {selectedTemplate ? (
              <>
                <Alert
                  type="success"
                  showIcon
                  message={`${selectedTemplate.name}｜题量 ${selectedTemplate.question_count} 题`}
                  description={`教材：${selectedTemplateMaterialDisplayName || selectedTemplate.material_version_id}；掌握比例 ${Number(selectedTemplate?.mastery_ratio?.掌握 || 0)}:${Number(selectedTemplate?.mastery_ratio?.熟悉 || 0)}:${Number(selectedTemplate?.mastery_ratio?.了解 || 0)}；路由 ${Number(selectedTemplate?.route_rules?.length || 0)} 条`}
                />
                <Card size="small" title="快捷开始">
                  <Space wrap style={{ width: '100%' }}>
                    <Input
                      value={selectedTemplateMaterialDisplayName || String(selectedTemplate?.material_version_id || '')}
                      readOnly
                      style={{ width: 420 }}
                      addonBefore="教材"
                    />
                    <Typography.Text type="secondary">
                      出题会直接使用模板绑定教材。
                    </Typography.Text>
                  </Space>
                </Card>
              </>
            ) : (
              <Alert type="warning" showIcon message="请选择一个出题模板。若还没有模板，请先去“出题模板”页面配置。" />
            )}
          </Space>
        ) : (
        <Space wrap style={{ width: '100%' }}>
          <Typography.Text style={{ color: 'rgba(0, 0, 0, 0.88)', fontWeight: 500 }}>切片范围：</Typography.Text>
          <Select
            value={materialVersionId}
            style={{ width: 260 }}
            placeholder="教材版本"
            onChange={(v) => {
              setMaterialVersionId(v);
              setHasQueried(false);
              setSelectedSliceKeys([]);
              setRows([]);
              setRunTrace([]);
              setSelectedGeneratedKeys([]);
              setErrors([]);
              setStats({ generated_count: 0, saved_count: 0 });
              setSliceViewMode('select');
              setShowSlicePanel(false);
              setAutoSelectAllOnNextFilter(false);
              setSelectedMastery([]);
              setAppliedSelectedMastery([]);
              loadApprovedSlices(tenantId, v);
              loadMaterialSliceTotal(tenantId, v);
              loadPathTree(tenantId, v);
            }}
            options={materials.map((m) => ({
              label: materialLabel(m),
              value: m.material_version_id,
            }))}
          />
          <Cascader
            style={{ width: 420 }}
            value={selectedPathNodes}
            options={withAllOption(pathTreeOptions)}
            showSearch={{
              filter: (input, path) =>
                path.some((option) => String(option.label || '').toLowerCase().includes(String(input || '').toLowerCase())),
            }}
            changeOnSelect
            allowClear
            placeholder="多级路径筛选（章节联动）"
            onChange={(vals) => setSelectedPathNodes(vals || [])}
            displayRender={(labels) => {
              const visible = labels.filter((x) => x !== '全选');
              if (visible.length === 0 && labels.includes('全选')) return '全选';
              return visible.join(' / ');
            }}
          />
          <Input
            value={sliceKeyword}
            onChange={(e) => setSliceKeyword(e.target.value)}
            placeholder="切片关键词（内容/路径）"
            style={{ width: 260 }}
          />
          <Select
            mode="multiple"
            allowClear
            placeholder="全部掌握程度/选择具体掌握程度"
            style={{ minWidth: 260 }}
            value={selectedMastery}
            options={[
              { label: '全部掌握程度', value: ALL_MASTERY_VALUE },
              ...masteryOptions.map((m) => ({ label: m, value: m })),
            ]}
            onChange={(vals) => {
              const next = Array.isArray(vals) ? vals : [];
              if (next.includes(ALL_MASTERY_VALUE)) {
                setSelectedMastery([ALL_MASTERY_VALUE]);
                return;
              }
              setSelectedMastery(next);
            }}
          />
          <Select
            value={selectedCalcSliceFilter || undefined}
            allowClear
            placeholder="计算题切片"
            style={{ width: 140 }}
            options={[
              { label: '全部', value: '' },
              { label: '仅计算题', value: 'yes' },
              { label: '仅非计算题', value: 'no' },
            ]}
            onChange={(v) => setSelectedCalcSliceFilter(v || '')}
          />
          <Button type="primary" onClick={onQueryFilters}>查询</Button>
          <Button
            onClick={() => {
              setSelectedPathNodes([]);
              setSliceKeyword('');
              setSelectedMastery([]);
              setSelectedCalcSliceFilter('');
              setAppliedPathNodes([]);
              setAppliedSliceKeyword('');
              setAppliedSelectedMastery([]);
              setAppliedCalcSliceFilter('');
              setHasQueried(false);
              setSelectedSliceKeys([]);
              setRows([]);
              setRunTrace([]);
              setSelectedGeneratedKeys([]);
              setErrors([]);
              setStats({ generated_count: 0, saved_count: 0 });
              setSliceViewMode('select');
              setShowSlicePanel(false);
              setAutoSelectAllOnNextFilter(false);
            }}
          >
            重置
          </Button>
          <Space size={4}>
            <Typography.Text type="secondary">可出题切片：</Typography.Text>
            <Typography.Link
              strong
              onClick={() => {
                if (!hasQueried) {
                  message.info('请先点击“查询”加载筛选结果');
                  return;
                }
                setShowSlicePanel((v) => !v);
                setSliceViewMode('content');
              }}
            >
              {finalSlices.length}
            </Typography.Link>
            <Typography.Text type="secondary">条</Typography.Text>
          </Space>
        </Space>
        )}
        {generateBy === 'manual' && finalSlices.length === 0 && (
          <Alert
            style={{ marginTop: 10 }}
            type="warning"
            showIcon
            message={zeroReason}
          />
        )}
        {generateBy === 'manual' && approvedSlices.length > 0 && pathTreeOptions.length === 0 && (
          <Alert
            style={{ marginTop: 10 }}
            type="warning"
            showIcon
            message="已加载到已通过切片，但路径下拉为空，请点击“查询”重拉一次；若仍为空请重启前后端。"
          />
        )}
      </Card>
      )}

      {pageMode === 'create' && (
        <Row gutter={12}>
          {generateBy === 'manual' && hasQueried && showSlicePanel && (
            <Col xs={24} lg={11}>
              <Card
                title={`出题知识切片（当前筛选 ${finalSlices.length} 条）`}
                extra={(
                  <Space size={8}>
                    <Segmented
                      size="small"
                      value={sliceViewMode}
                      onChange={setSliceViewMode}
                      options={[
                        { label: '表格视图', value: 'select' },
                        { label: '内容视图', value: 'content' },
                      ]}
                    />
                    <Typography.Text type="secondary">已选 {selectedSliceKeys.length} 条</Typography.Text>
                    <Button
                      size="small"
                      onClick={() => setSelectedSliceKeys(finalSlices.map((x) => String(x.slice_id)))}
                      disabled={!finalSlices.length}
                    >
                      全选
                    </Button>
                    <Button size="small" onClick={() => setSelectedSliceKeys([])}>清空</Button>
                  </Space>
                )}
                style={{ marginBottom: 12 }}
              >
                {sliceViewMode === 'content' ? (
                  <Space direction="vertical" style={{ width: '100%' }} size={10}>
                    {pagedContentSlices.map((item) => {
                      const key = String(item.slice_id);
                      const checked = selectedSliceKeys.includes(key);
                      return (
                        <Card key={key} size="small" styles={{ body: { padding: 12 } }}>
                          <Space direction="vertical" style={{ width: '100%' }} size={10}>
                            <Space align="start" wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                              <Space align="start" wrap>
                                <Checkbox
                                  checked={checked}
                                  onChange={(e) => {
                                    const on = !!e?.target?.checked;
                                    setSelectedSliceKeys((prev) => {
                                      const set = new Set(prev.map(String));
                                      if (on) set.add(key);
                                      else set.delete(key);
                                      return Array.from(set);
                                    });
                                  }}
                                />
                                <Tag color="green">已通过</Tag>
                                <Typography.Text strong>
                                  ID: {item.slice_id} | {String(item.path || '（无路径）')}
                                </Typography.Text>
                              </Space>
                              <Typography.Text type="secondary">掌握程度：{item.mastery || '未知'}</Typography.Text>
                            </Space>
                            <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                              {String(item.slice_content || item.preview || '（无切片内容）')}
                            </Typography.Paragraph>
                          </Space>
                        </Card>
                      );
                    })}
                    <div style={{ textAlign: 'right' }}>
                      <Pagination
                        current={sliceContentPage}
                        pageSize={contentPageSize}
                        total={finalSlices.length}
                        onChange={(p) => setSliceContentPage(p)}
                        showSizeChanger={false}
                      />
                    </div>
                  </Space>
                ) : (
                  <Table
                    rowKey={(record) => String(record.slice_id)}
                    rowSelection={{
                      selectedRowKeys: selectedSliceKeys,
                      onChange: (keys) => setSelectedSliceKeys(keys),
                    }}
                    columns={slicePreviewColumns}
                    dataSource={finalSlices}
                    pagination={{ pageSize: 8, showSizeChanger: false }}
                    size="small"
                  />
                )}
              </Card>
            </Col>
          )}
          <Col xs={24} lg={generateBy === 'manual' && hasQueried && showSlicePanel ? 13 : 24}>
            <Card style={{ marginBottom: 12 }} title="出题设置">
              <Form form={createForm} layout="inline" onFinish={onSubmit}>
                <Form.Item
                  name="task_name"
                  label="任务名称"
                  rules={[{ required: true, whitespace: true, message: '请输入任务名称' }]}
                >
                  <Input placeholder="例如：武汉-单选抽测-第1批" style={{ width: 240 }} />
                </Form.Item>
                <Form.Item label="出题范围">
                  <Select
                    value={genScopeMode}
                    style={{ width: 180 }}
                    onChange={setGenScopeMode}
                    disabled={generateBy === 'template'}
                    options={[
                      { label: '自定义题量', value: 'custom' },
                      { label: '每个知识点各出一题', value: 'per_slice' },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="num_questions" initialValue={1} label="题量">
                  <InputNumber
                    min={1}
                    max={200}
                    disabled={generateBy === 'template' || genScopeMode === 'per_slice'}
                  />
                </Form.Item>
                <Form.Item name="question_type" initialValue="单选题" label="题型">
                  <Select
                    style={{ width: 140 }}
                    options={[
                      { label: '单选题', value: '单选题' },
                      { label: '多选题', value: '多选题' },
                      { label: '判断题', value: '判断题' },
                      { label: '随机', value: '随机' },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="generation_mode" initialValue="随机" label="筛选条件">
                  <Select
                    style={{ width: 220 }}
                    options={[
                      { label: '基础概念/理解记忆', value: '基础概念/理解记忆' },
                      { label: '实战应用/推演', value: '实战应用/推演' },
                      { label: '随机', value: '随机' },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="difficulty" initialValue="随机" label="难度">
                  <Select
                    style={{ width: 160 }}
                    options={[
                      { label: '随机', value: '随机' },
                      { label: '简单 (0.3-0.5)', value: '简单 (0.3-0.5)' },
                      { label: '中等 (0.5-0.7)', value: '中等 (0.5-0.7)' },
                      { label: '困难 (0.7-0.9)', value: '困难 (0.7-0.9)' },
                    ]}
                  />
                </Form.Item>
                <Form.Item>
                  <Button
                    type="primary"
                    htmlType="submit"
                    loading={taskLoading}
                    disabled={generateBy === 'template'
                      ? !selectedTemplate
                      : (!finalSlices.length || !selectedSliceKeys.length)}
                  >
                    开始出题
                  </Button>
                </Form.Item>
              </Form>
            </Card>

            {showResultInCreateMode && (
              <Card
                title={loading ? '出题过程' : `结果：生成 ${stats.generated_count} 题，已入库 ${stats.saved_count} 题`}
                extra={(
                  <Space>
                    {loading && activeTaskId && (
                      <Button
                        danger
                        size="small"
                        loading={cancellingTaskId === activeTaskId}
                        disabled={!!cancellingTaskId}
                        onClick={async () => {
                          if (!tenantId || !activeTaskId) return;
                          setCancellingTaskId(activeTaskId);
                          try {
                            const res = await cancelGenerateTask(tenantId, activeTaskId);
                            message.success(res?.message || '已请求取消');
                            loadTaskList(tenantId, true).catch(() => {});
                          } catch (e) {
                            message.error(e?.response?.data?.error?.message || e?.message || '取消失败');
                          } finally {
                            setCancellingTaskId('');
                          }
                        }}
                      >
                        取消任务
                      </Button>
                    )}
                    <Segmented
                      size="small"
                      value={traceDetailMode}
                      onChange={(v) => setTraceDetailMode(String(v || 'concise'))}
                      options={[
                        { label: '过程精简', value: 'concise' },
                        { label: '过程详细', value: 'full' },
                      ]}
                    />
                    {!loading && !AUTO_SAVE_PASSED_QUESTIONS && (
                      <>
                        <Typography.Text type="secondary">已选 {selectedGeneratedKeys.length} 题</Typography.Text>
                        <Button type="primary" onClick={onSaveSelectedToBank} loading={savingToBank}>入库所选</Button>
                      </>
                    )}
                  </Space>
                )}
                style={{ marginBottom: 12 }}
              >
                {loading ? (
                  runTrace.length === 0 ? (
                    <Typography.Text type="secondary">正在启动出题流程，请稍候...</Typography.Text>
                  ) : (
                    <>
                      {(() => {
                        const lastItem = orderedRunTrace[orderedRunTrace.length - 1];
                        const status = getLatestRunStatus(lastItem?.steps || []);
                        if (status.latestRunHasWriterDone && !status.latestRunHasCriticTerminal) {
                          return (
                            <Alert
                              type="info"
                              showIcon
                              message="Critic 审核中"
                              description="当前正在执行审核（可读性检查 + 质量验证，可能含计算题代码校验），通常需 1～3 分钟，请勿关闭页面。若使用推理模型（如 deepseek-reasoner）可能更久。"
                              style={{ marginBottom: 12 }}
                            />
                          );
                        }
                        return null;
                      })()}
                      <Collapse
                      items={orderedRunTrace.map((item, idx) => ({
                        key: getTraceItemKey(item, idx),
                        label: `第 ${getTraceDisplayIndex(item, idx + 1)} 题 | 切片 ${item.slice_id} | 耗时 ${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`,
                        children: renderTracePanelBody(item),
                      }))}
                    />
                    </>
                  )
                ) : (
                  <Space direction="vertical" style={{ width: '100%' }} size={12}>
                    {runTrace.length > 0 && (
                      <Collapse
                        items={orderedRunTrace.map((item, idx) => ({
                          key: getTraceItemKey(item, idx),
                          label: `第 ${getTraceDisplayIndex(item, idx + 1)} 题 | 切片 ${item.slice_id} | 耗时 ${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`,
                          children: renderTracePanelBody(item),
                        }))}
                      />
                    )}
                    <Table
                      rowKey={(record) => String(record._gen_key || '')}
                      rowSelection={
                        AUTO_SAVE_PASSED_QUESTIONS
                          ? undefined
                          : {
                            selectedRowKeys: selectedGeneratedKeys,
                            onChange: (keys) => setSelectedGeneratedKeys(keys),
                          }
                      }
                      columns={columns}
                      dataSource={rows}
                      pagination={{ pageSize: 10 }}
                    />
                    {errors.length > 0 && (
                      <Space direction="vertical" style={{ width: '100%' }} size={8}>
                        {errors.map((e, i) => (
                          <Alert key={i} type="error" message={e} />
                        ))}
                      </Space>
                    )}
                  </Space>
                )}
              </Card>
            )}
          </Col>
        </Row>
      )}
      <Modal
        title="题目详情"
        open={viewQuestionOpen}
        onCancel={() => {
          setViewQuestionOpen(false);
          setViewQuestionRecord(null);
        }}
        footer={null}
        width={900}
      >
        <QuestionDetailView question={viewQuestionRecord || {}} />
      </Modal>
    </>
  );
}
