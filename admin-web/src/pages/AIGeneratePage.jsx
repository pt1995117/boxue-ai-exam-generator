import React, { useEffect, useMemo, useState } from 'react';
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
  Typography,
} from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  addBankQuestions,
  createGenerateTask,
  getGenerateTask,
  getSliceImageUrl,
  getSlicePathTree,
  getSlices,
  listGenerateTasks,
  listMaterials,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import MarkdownWithMermaid from '../components/MarkdownWithMermaid';
import QuestionDetailView from '../components/QuestionDetailView';

export default function AIGeneratePage() {
  const AUTO_SAVE_PASSED_QUESTIONS = true;
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
  const [errors, setErrors] = useState([]);
  const [stats, setStats] = useState({ generated_count: 0, saved_count: 0 });
  const [viewQuestionOpen, setViewQuestionOpen] = useState(false);
  const [viewQuestionRecord, setViewQuestionRecord] = useState(null);
  const [taskItems, setTaskItems] = useState([]);
  const [taskKeyword, setTaskKeyword] = useState('');
  const [taskStatusFilter, setTaskStatusFilter] = useState('');
  const [taskMaterialFilter, setTaskMaterialFilter] = useState('');
  const [taskQueryKeyword, setTaskQueryKeyword] = useState('');
  const [taskQueryStatus, setTaskQueryStatus] = useState('');
  const [taskQueryMaterial, setTaskQueryMaterial] = useState('');
  const [activeTaskId, setActiveTaskId] = useState('');
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('');
  const [approvedSlices, setApprovedSlices] = useState([]);
  const [materialSliceTotal, setMaterialSliceTotal] = useState(0);
  const [pathTreeOptions, setPathTreeOptions] = useState([]);
  const [selectedPathNodes, setSelectedPathNodes] = useState([]);
  const [sliceKeyword, setSliceKeyword] = useState('');
  const [selectedMastery, setSelectedMastery] = useState([]);
  const [appliedPathNodes, setAppliedPathNodes] = useState([]);
  const [appliedSliceKeyword, setAppliedSliceKeyword] = useState('');
  const [appliedSelectedMastery, setAppliedSelectedMastery] = useState([]);
  const [hasQueried, setHasQueried] = useState(false);
  const [selectedSliceKeys, setSelectedSliceKeys] = useState([]);
  const [sliceContentPage, setSliceContentPage] = useState(1);
  const [sliceViewMode, setSliceViewMode] = useState('select');
  const [showSlicePanel, setShowSlicePanel] = useState(false);
  const [autoSelectAllOnNextFilter, setAutoSelectAllOnNextFilter] = useState(false);
  const [genScopeMode, setGenScopeMode] = useState('custom');
  const [traceDetailMode, setTraceDetailMode] = useState('concise');
  const ALL_NODE_PREFIX = '__ALL__::';
  const conciseTraceKeywords = [
    '开始出题',
    '路由',
    '初稿',
    '定稿',
    '题干要点',
    '题目结果',
    '审核',
    '必改项',
    '修复',
    '计算结果',
    '稳定性预警',
    '题目生成成功',
  ];
  const getVisibleSteps = (steps) => {
    const all = Array.isArray(steps) ? steps : [];
    if (traceDetailMode === 'full') return all;
    return all.filter((step) => {
      const msg = String(step?.message || '');
      const detail = String(step?.detail || '');
      if (step?.level === 'error' || step?.level === 'warning' || step?.level === 'success') return true;
      if (conciseTraceKeywords.some((kw) => msg.includes(kw))) return true;
      if (conciseTraceKeywords.some((kw) => detail.includes(kw))) return true;
      return false;
    });
  };
  const splitOptionLines = (detail) => {
    const raw = String(detail || '').trim();
    if (!raw) return [];
    return raw.split(/\s*\|\s*/).map((x) => String(x || '').trim()).filter(Boolean);
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
    if (msg.includes('题干')) return { phase, field: 'stem', detail };
    if (msg.includes('选项')) return { phase, field: 'options', detail };
    if (msg.includes('解析')) return { phase, field: 'explanation', detail };
    if (msg === '题目结果') return { phase, field: 'result', detail };
    return null;
  };
  const renderNormalStep = (item, step, idx) => {
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
      <Space key={`${item.index}_${step.seq || idx}`} size={8} wrap>
        <Tag color={nodeColor}>{step.node || 'system'}</Tag>
        <Typography.Text>{step.message}</Typography.Text>
        {step.detail ? (
          <Typography.Paragraph type="secondary" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
            {step.detail}
          </Typography.Paragraph>
        ) : null}
      </Space>
    );
  };
  const renderQuestionGroup = (item, group, idx) => {
    const optionLines = splitOptionLines(group.options || '').map((line, i) => normalizeOptionLine(line, i));
    return (
      <div
        key={`${item.index}_qg_${group.phase}_${idx}`}
        style={{
          border: '1px solid #e5e6eb',
          borderRadius: 8,
          padding: '10px 12px',
          background: '#fafcff',
        }}
      >
        <Typography.Text strong>{group.phase}</Typography.Text>
        {group.stem ? (
          <Typography.Paragraph style={{ margin: '8px 0 0 0', whiteSpace: 'pre-wrap' }}>
            {group.stem}
          </Typography.Paragraph>
        ) : null}
        {optionLines.length > 0 ? (
          <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 6 }}>
            {optionLines.map((line, i) => (
              <Typography.Text key={`${item.index}_qg_${group.phase}_opt_${i}`}>{line}</Typography.Text>
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
    const steps = getVisibleSteps(item.steps);
    const rows = [];
    const groupByKey = new Map();
    steps.forEach((step, idx) => {
      const parsed = parseQuestionStep(step);
      if (!parsed) {
        rows.push({ type: 'normal', step, idx });
        return;
      }
      const key = `${step?.node || 'system'}|${parsed.phase}`;
      let group = groupByKey.get(key);
      if (!group) {
        group = {
          node: step?.node || 'system',
          phase: parsed.phase,
          stem: '',
          options: '',
          explanation: '',
          result: '',
        };
        groupByKey.set(key, group);
        rows.push({ type: 'question_group', group });
      }
      if (parsed.field === 'stem') group.stem = parsed.detail;
      if (parsed.field === 'options') group.options = parsed.detail;
      if (parsed.field === 'explanation') group.explanation = parsed.detail;
      if (parsed.field === 'result') group.result = parsed.detail;
    });
    return rows.map((row, i) => (
      row.type === 'normal'
        ? renderNormalStep(item, row.step, row.idx)
        : renderQuestionGroup(item, row.group, i)
    ));
  };
  const approvedSliceById = useMemo(() => {
    const m = new Map();
    (approvedSlices || []).forEach((row) => {
      const sid = Number(row?.slice_id || 0);
      if (!sid) return;
      m.set(sid, row);
    });
    return m;
  }, [approvedSlices]);
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
  const materialLabel = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    const name = raw.replace(/^v\d{8}_\d{6}_/, '') || raw || m?.material_version_id;
    return `${name}${m?.status === 'effective' ? '（当前生效）' : ''}`;
  };
  const formatTime = (value) => {
    const s = String(value || '').trim();
    if (!s) return '-';
    return s.replace('T', ' ').replace(/\.\d+\+\d{2}:\d{2}$/, '');
  };
  const isTaskRunning = (status) => ['pending', 'running'].includes(String(status || ''));
  const applyTaskDetail = (task) => {
    const t = task && typeof task === 'object' ? task : {};
    setLoading(isTaskRunning(t.status));
    const items = Array.isArray(t.items) ? t.items : [];
    setRows(items.map((item, idx) => ({ ...item, _gen_key: `${String(t.task_id || 'task')}_${idx}` })));
    setRunTrace(Array.isArray(t.process_trace) ? t.process_trace : []);
    setErrors(Array.isArray(t.errors) ? t.errors : []);
    setStats({
      generated_count: Number(t.generated_count || 0),
      saved_count: Number(t.saved_count || 0),
    });
  };
  const loadTaskList = async (tid, keepActive = true) => {
    if (!tid) return [];
    const res = await listGenerateTasks(tid, { limit: 100 });
    const items = Array.isArray(res?.items) ? res.items : [];
    setTaskItems(items);
    if (!items.length) {
      if (!keepActive) setActiveTaskId('');
      return [];
    }
    if (!keepActive || !activeTaskId) {
      setActiveTaskId(String(items[0].task_id || ''));
      return items;
    }
    const hasActive = items.some((x) => String(x?.task_id || '') === String(activeTaskId));
    if (!hasActive) setActiveTaskId(String(items[0].task_id || ''));
    return items;
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

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    if (!tenantId) return;
    setPageMode('tasks');
    setHasQueried(false);
    setSelectedSliceKeys([]);
    setRows([]);
    setRunTrace([]);
    setTaskItems([]);
    setActiveTaskId('');
    setSelectedGeneratedKeys([]);
    setErrors([]);
    setStats({ generated_count: 0, saved_count: 0 });
    setShowSlicePanel(false);
    setAutoSelectAllOnNextFilter(false);
    loadMaterials(tenantId);
    loadTaskList(tenantId, false).catch(() => setTaskItems([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  useEffect(() => {
    if (!tenantId || !activeTaskId) return undefined;
    if (pageMode !== 'create') return undefined;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await getGenerateTask(tenantId, activeTaskId);
        if (cancelled) return;
        const task = res?.task || {};
        applyTaskDetail(task);
        if (isTaskRunning(task?.status)) {
          setTimeout(tick, 1200);
        } else {
          loadTaskList(tenantId, true).catch(() => {});
        }
      } catch (_e) {
        if (!cancelled) setTimeout(tick, 2000);
      }
    };
    tick();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, activeTaskId, pageMode]);

  useEffect(() => {
    if (!tenantId) return undefined;
    const timer = setInterval(() => {
      loadTaskList(tenantId, true).catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

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
      message.error(e?.response?.data?.error?.message || '加载已审核切片失败');
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
      const activeItems = items.filter((x) => String(x?.status || '') !== 'archived');
      setMaterials(activeItems);
      const effective = activeItems.find((x) => x.status === 'effective');
      const chosen = (effective || activeItems[0] || {}).material_version_id || '';
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
      setMaterialVersionId('');
      setApprovedSlices([]);
      setMaterialSliceTotal(0);
      setPathTreeOptions([]);
      message.error(e?.response?.data?.error?.message || '加载教材版本失败');
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
      if (!candidateIds.length) {
        message.warning('请先在左侧勾选至少一个知识切片');
        setLoading(false);
        return;
      }
      const payload = {
        task_name: taskName,
        gen_scope_mode: genScopeMode,
        num_questions: genScopeMode === 'per_slice' ? candidateIds.length : (values.num_questions || 1),
        question_type: values.question_type || '单选题',
        generation_mode: values.generation_mode || '随机',
        difficulty: values.difficulty || '随机',
        save_to_bank: AUTO_SAVE_PASSED_QUESTIONS,
        slice_ids: candidateIds,
        material_version_id: materialVersionId || undefined,
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
  const finalSlices = chapterFiltered.filter((s) => (
    !selectedMasteryForFilter.length ? true : selectedMasteryForFilter.includes(s.mastery)
  ));
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
    if (finalSlices.length === 0) return '当前掌握程度筛选后没有可出题切片，请调整掌握程度条件。';
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
      width: 100,
      render: (_, r) => (
        <Button
          size="small"
          onClick={() => navigate(`/ai-generate/tasks/${encodeURIComponent(String(r?.task_id || ''))}`)}
        >
          查看
        </Button>
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
  return (
    <>
      {pageMode === 'tasks' && (
        <>
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
                <Button type="primary" onClick={() => setPageMode('create')}>新建出题任务</Button>
              </Space>
            </div>
          </Card>

          <Card style={{ marginBottom: 12 }}>
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
          <Button type="primary" onClick={onQueryFilters}>查询</Button>
          <Button
            onClick={() => {
              setSelectedPathNodes([]);
              setSliceKeyword('');
              setSelectedMastery([]);
              setAppliedPathNodes([]);
              setAppliedSliceKeyword('');
              setAppliedSelectedMastery([]);
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
        {finalSlices.length === 0 && (
          <Alert
            style={{ marginTop: 10 }}
            type="warning"
            showIcon
            message={zeroReason}
          />
        )}
        {approvedSlices.length > 0 && pathTreeOptions.length === 0 && (
          <Alert
            style={{ marginTop: 10 }}
            type="warning"
            showIcon
            message="已加载到已通过切片，但路径下拉为空，请点击“查询”重拉一次；若仍为空请重启前后端。"
          />
        )}
      </Card>
      )}

      {pageMode === 'create' && hasQueried && (
        <Row gutter={12}>
          {showSlicePanel && (
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
                        <Card key={key} size="small" bodyStyle={{ padding: 12 }}>
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
          <Col xs={24} lg={showSlicePanel ? 13 : 24}>
            <Card style={{ marginBottom: 12 }} title="出题设置">
              <Form layout="inline" onFinish={onSubmit}>
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
                    options={[
                      { label: '自定义题量', value: 'custom' },
                      { label: '每个知识点各出一题', value: 'per_slice' },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="num_questions" initialValue={1} label="题量">
                  <InputNumber min={1} max={200} disabled={genScopeMode === 'per_slice'} />
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
                    disabled={!finalSlices.length || !selectedSliceKeys.length}
                  >
                    开始出题
                  </Button>
                </Form.Item>
              </Form>
            </Card>

            {hasGenerationSession && (
              <Card
                title={loading ? '出题过程' : `结果：生成 ${stats.generated_count} 题，已入库 ${stats.saved_count} 题`}
                extra={(
                  <Space>
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
                    <Collapse
                      items={runTrace.map((item) => ({
                        key: String(item.index),
                        label: `第 ${item.index} 题 | 切片 ${item.slice_id} | 耗时 ${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`,
                        children: (
                          <Space direction="vertical" style={{ width: '100%' }} size={4}>
                            <Typography.Text type="secondary">{item.slice_path || '（无路径）'}</Typography.Text>
                            <div style={{ maxHeight: 320, overflow: 'auto' }}>
                              <MarkdownWithMermaid text={buildTraceSliceMarkdown(item)} />
                            </div>
                            {renderTraceEntries(item)}
                          </Space>
                        ),
                      }))}
                    />
                  )
                ) : (
                  <Space direction="vertical" style={{ width: '100%' }} size={12}>
                    {runTrace.length > 0 && (
                      <Collapse
                        items={runTrace.map((item) => ({
                          key: String(item.index),
                          label: `第 ${item.index} 题 | 切片 ${item.slice_id} | 耗时 ${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`,
                          children: (
                            <Space direction="vertical" style={{ width: '100%' }} size={4}>
                              <Typography.Text type="secondary">{item.slice_path || '（无路径）'}</Typography.Text>
                              <div style={{ maxHeight: 320, overflow: 'auto' }}>
                                <MarkdownWithMermaid text={buildTraceSliceMarkdown(item)} />
                              </div>
                              {renderTraceEntries(item)}
                            </Space>
                          ),
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
