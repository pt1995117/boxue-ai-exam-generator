import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Card, Form, Input, message, Select, Space, Table, Tag, Tooltip, Typography } from 'antd';
import { Link } from 'react-router-dom';
import { createJudgeTask, listGenerateTaskJudgeBankItems, listGenerateTasks, listJudgeTasks, listQaRuns, listQaRunsQuick, previewJudgeRuns } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const isActive = (status) => ['pending', 'running'].includes(String(status || '').toLowerCase());

function toTenPointScore(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return n > 10 ? n / 10 : n;
}

function getJudgeBaselineScore(offlineJudge) {
  if (!offlineJudge || typeof offlineJudge !== 'object') return null;
  if (offlineJudge.baseline_score != null) return Number(offlineJudge.baseline_score);
  if (offlineJudge.penalty_score != null) return Number(offlineJudge.penalty_score);
  return null;
}

export default function JudgeTaskPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [pageMode, setPageMode] = useState('tasks'); // tasks | create
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [taskItems, setTaskItems] = useState([]);
  const [sourceTasks, setSourceTasks] = useState([]);
  const [allRuns, setAllRuns] = useState([]);
  const [runs, setRuns] = useState([]);
  const [taskKeyword, setTaskKeyword] = useState('');
  const [taskStatusFilter, setTaskStatusFilter] = useState('');
  const [queryKeyword, setQueryKeyword] = useState('');
  const [queryStatus, setQueryStatus] = useState('');
  const [selectedQuestions, setSelectedQuestions] = useState([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [sourceTaskBankMap, setSourceTaskBankMap] = useState({});
  const [form] = Form.useForm();
  const selectedRunIds = Form.useWatch('run_ids', form) || [];
  const selectedSourceTaskIds = Form.useWatch('source_task_ids', form) || [];
  const tasksLoadingRef = useRef(false);
  const allLoadingRef = useRef(false);

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadRuns = async (tid) => {
    if (!tid) return;
    let res = null;
    try {
      res = await listQaRunsQuick(tid, { days: 90, success_only: 0, limit: 200 });
    } catch (e) {
      res = await listQaRuns(tid, { days: 90, success_only: 0, page: 1, page_size: 200 });
    }
    const items = Array.isArray(res?.items) ? res.items : [];
    setAllRuns(items);
    setRuns(items.filter((x) => Number(x?.saved_count || 0) > 0));
  };

  const loadTasks = async (tid) => {
    if (!tid) return;
    if (tasksLoadingRef.current) return;
    tasksLoadingRef.current = true;
    try {
      const res = await listJudgeTasks(tid, { limit: 200 });
      const items = Array.isArray(res?.items) ? res.items : [];
      setTaskItems(items);
    } finally {
      tasksLoadingRef.current = false;
    }
  };

  const loadSourceTasks = async (tid) => {
    if (!tid) return;
    const res = await listGenerateTasks(tid, { limit: 200 });
    const items = Array.isArray(res?.items) ? res.items : [];
    setSourceTasks(items.filter((x) => Number(x?.saved_count || 0) > 0));
  };

  const loadAll = async (tid) => {
    if (!tid) return;
    if (allLoadingRef.current) return;
    allLoadingRef.current = true;
    setLoading(true);
    try {
      await Promise.all([loadRuns(tid), loadTasks(tid), loadSourceTasks(tid)]);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载 Judge 任务失败');
    } finally {
      allLoadingRef.current = false;
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!tenantId) return;
    loadAll(tenantId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  useEffect(() => {
    if (!tenantId) return undefined;
    const timer = window.setInterval(() => {
      loadTasks(tenantId).catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [tenantId]);

  const runMap = useMemo(() => {
    const m = new Map();
    for (const r of allRuns || []) {
      const rid = String(r?.run_id || '').trim();
      if (!rid) continue;
      m.set(rid, r);
    }
    return m;
  }, [allRuns]);

  const sourceTaskMap = useMemo(() => {
    const m = new Map();
    for (const t of sourceTasks || []) {
      const tid = String(t?.task_id || '').trim();
      if (!tid) continue;
      m.set(tid, t);
    }
    return m;
  }, [sourceTasks]);

  const getCanonicalRunIds = (runIds) => {
    return Array.from(new Set(
      (Array.isArray(runIds) ? runIds : [])
        .map((ridRaw) => String(ridRaw || '').trim())
        .filter(Boolean)
    ));
  };

  const filteredTasks = useMemo(() => {
    return (taskItems || []).filter((t) => {
      const status = String(t?.status || '').toLowerCase();
      const judgeName = String(t?.task_name || '').toLowerCase();
      const sourceName = String(t?.source_task_name || '').toLowerCase();
      const rid = String(t?.run_id || '').toLowerCase();
      if (queryStatus && status !== queryStatus) return false;
      if (queryKeyword) {
        const q = queryKeyword.toLowerCase();
        if (!judgeName.includes(q) && !sourceName.includes(q) && !rid.includes(q)) return false;
      }
      return true;
    });
  }, [taskItems, queryKeyword, queryStatus]);

  useEffect(() => {
    let cancelled = false;
    const loadSourceTaskBankItems = async () => {
      if (!tenantId || !Array.isArray(selectedSourceTaskIds) || !selectedSourceTaskIds.length) {
        setSourceTaskBankMap({});
        return;
      }
      const nextMap = {};
      for (const taskIdRaw of selectedSourceTaskIds) {
        const taskId = String(taskIdRaw || '').trim();
        if (!taskId) continue;
        try {
          // eslint-disable-next-line no-await-in-loop
          const res = await listGenerateTaskJudgeBankItems(tenantId, taskId);
          nextMap[taskId] = Array.isArray(res?.items) ? res.items : [];
        } catch (e) {
          nextMap[taskId] = [];
        }
      }
      if (!cancelled) setSourceTaskBankMap(nextMap);
    };
    loadSourceTaskBankItems();
    return () => {
      cancelled = true;
    };
  }, [selectedSourceTaskIds, tenantId]);

  const sourceTaskOptions = useMemo(() => {
    return (sourceTasks || []).map((t) => {
      const tid = String(t?.task_id || '');
      const name = String(t?.task_name || '').trim() || tid;
      const savedCount = Number(t?.saved_count || 0);
      const endedAt = String(t?.ended_at || t?.updated_at || '').slice(0, 19);
      return {
        label: `${name} | 可Judge=${savedCount} | ${endedAt}`,
        value: tid,
      };
    });
  }, [sourceTasks]);

  const runOptions = useMemo(() => {
    return (runs || []).map((r) => {
      const rid = String(r?.run_id || '');
      const sourceTask = sourceTaskMap.get(String(r?.task_id || '').trim());
      const sourceName = String(sourceTask?.task_name || '').trim();
      const name = sourceName || String(r?.task_name || '').trim() || String(r?.task_id || '').trim() || rid;
      return {
        label: `${name} | ${rid} | saved=${Number(r?.saved_count || 0)} | ${String(r?.ended_at || '').slice(0, 19)}`,
        value: rid,
      };
    });
  }, [runs, sourceTaskMap]);

  const taskColumns = [
    {
      title: '出题任务名',
      dataIndex: 'source_task_name',
      width: 260,
      render: (v, r) => <Link to={`/judge-tasks/${encodeURIComponent(String(r?.task_id || ''))}`}>{String(v || runMap.get(String(r?.run_id || ''))?.task_name || '-') || '-'}</Link>,
    },
    { title: 'Judge任务名', dataIndex: 'task_name', width: 200, ellipsis: true },
    { title: 'task_id', dataIndex: 'task_id', width: 240, ellipsis: true },
    { title: 'run_id', dataIndex: 'run_id', width: 240, ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 120,
      render: (v) => {
        const s = String(v || '');
        const color = s === 'completed' ? 'green' : s === 'failed' ? 'red' : s === 'cancelled' ? 'orange' : s === 'running' ? 'blue' : 'default';
        return <Tag color={color}>{s || '-'}</Tag>;
      },
    },
    {
      title: '进度',
      dataIndex: 'progress',
      width: 120,
      render: (v, r) => `${Number(v?.current || r?.completed_count || 0)}/${Number(v?.total || r?.requested_count || 0)}`,
    },
    {
      title: '题数',
      width: 90,
      render: (_, r) => {
        const runSavedCount = Number(runMap.get(String(r?.run_id || ''))?.saved_count || 0);
        if (runSavedCount > 0) return runSavedCount;
        const bankIds = Array.isArray(r?.request?.bank_question_ids) ? r.request.bank_question_ids : [];
        if (bankIds.length > 0) return bankIds.length;
        return Number(r?.judge_count || r?.progress?.total || 0);
      },
    },
    {
      title: 'Judge结果',
      width: 220,
      render: (_, r) => {
        const dc = r?.decision_counts || {};
        const pass = Number(dc?.pass || 0);
        const review = Number(dc?.review || 0);
        const reject = Number(dc?.reject || 0);
        return (
          <Space size={4} wrap>
            <Tag color="green">pass {pass}</Tag>
            <Tag color="gold">review {review}</Tag>
            <Tag color="red">reject {reject}</Tag>
          </Space>
        );
      },
    },
    { title: '创建时间', dataIndex: 'created_at', width: 190 },
    { title: '更新时间', dataIndex: 'updated_at', width: 190 },
  ];

  useEffect(() => {
    let cancelled = false;
    const loadSelectedQuestions = async () => {
      if (pageMode !== 'create') return;
      const bankRows = Object.values(sourceTaskBankMap || {}).flat();
      if (bankRows.length) {
        const rows = bankRows.map((row, index) => ({
          ...row,
          run_seq: 1,
          q_seq: row?.q_seq ?? (index + 1),
          quality_score: toTenPointScore(row?.quality_score),
          baseline_score: toTenPointScore(row?.baseline_score),
        }));
        setSelectedQuestions(rows);
        return;
      }
      const runIds = Array.isArray(selectedRunIds)
        ? selectedRunIds.map((x) => String(x || '').trim()).filter(Boolean)
        : [];
      const canonicalRunIds = getCanonicalRunIds(runIds);
      if (!tenantId || !canonicalRunIds.length) {
        setSelectedQuestions([]);
        return;
      }
      setPreviewLoading(true);
      try {
        const res = await previewJudgeRuns(tenantId, { run_ids: canonicalRunIds });
        if (cancelled) return;
        const rows = Array.isArray(res?.items) ? res.items.map((row) => ({
          ...row,
          quality_score: toTenPointScore(row?.quality_score),
          baseline_score: toTenPointScore(row?.baseline_score),
        })) : [];
        setSelectedQuestions(rows);
      } catch (e) {
        if (!cancelled) {
          setSelectedQuestions([]);
          message.error(e?.response?.data?.error?.message || '加载已选 run 题目失败');
        }
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    };
    loadSelectedQuestions();
    return () => {
      cancelled = true;
    };
  }, [pageMode, selectedRunIds, tenantId, runMap, sourceTaskBankMap]);

  const selectedQuestionColumns = [
    { title: 'run序', dataIndex: 'run_seq', width: 70 },
    { title: '题序', dataIndex: 'q_seq', width: 70 },
    {
      title: 'run_id',
      dataIndex: 'run_id',
      width: 220,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={v || '-'}>
          <span>{v || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: 'question_id',
      dataIndex: 'question_id',
      width: 220,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={v || '-'}>
          <span>{v || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '题干',
      dataIndex: 'stem',
      width: 320,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{v || '-'}</span>}>
          <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '选项',
      dataIndex: 'options',
      width: 260,
      render: (arr) => {
        const lines = Array.isArray(arr) ? arr.map((x) => String(x || '').trim()).filter(Boolean) : [];
        const txt = lines.length ? lines.map((x, i) => `${String.fromCharCode(65 + i)}. ${x}`).join('\n') : '-';
        return (
          <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{txt}</span>}>
            <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{txt}</span>
          </Tooltip>
        );
      },
    },
    {
      title: '质量分',
      dataIndex: 'quality_score',
      width: 90,
      render: (v) => (v == null ? '-' : Number(v).toFixed(2)),
    },
    {
      title: '基准分',
      dataIndex: 'baseline_score',
      width: 90,
      render: (v) => (v == null ? '-' : Number(v).toFixed(2)),
    },
    { title: '答案', dataIndex: 'answer', width: 90, render: (v) => v || '-' },
    {
      title: '解析',
      dataIndex: 'explanation',
      width: 360,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{v || '-'}</span>}>
          <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '已落库',
      dataIndex: 'saved',
      width: 90,
      render: (v) => (v ? <Tag color="green">是</Tag> : <Tag color="default">否</Tag>),
    },
  ];

  const onSubmit = async (values) => {
    if (!tenantId) return;
    const name = String(values.task_name || '').trim();
    const selectedTaskIds = Array.isArray(values.source_task_ids) ? values.source_task_ids.map((x) => String(x || '').trim()).filter(Boolean) : [];
    const runIdsRaw = Array.isArray(values.run_ids) ? values.run_ids.map((x) => String(x || '').trim()).filter(Boolean) : [];
    const canonicalRunIds = getCanonicalRunIds(runIdsRaw);
    const bankTaskEntries = selectedTaskIds
      .map((taskId) => [taskId, Array.isArray(sourceTaskBankMap?.[taskId]) ? sourceTaskBankMap[taskId] : []])
      .filter(([, rows]) => rows.length > 0);
    const previewRunIds = Array.from(new Set((selectedQuestions || []).map((row) => String(row?.run_id || '').trim()).filter(Boolean)));
    const runIds = previewRunIds.length
      ? canonicalRunIds.filter((rid) => previewRunIds.includes(String(rid || '').trim()))
      : canonicalRunIds;
    const skippedRunIds = canonicalRunIds.filter((rid) => !runIds.includes(rid));
    if (!name) {
      message.warning('请输入任务名称');
      return;
    }
    if (!bankTaskEntries.length && !runIds.length) {
      message.warning('请至少选择一个出题任务或 run');
      return;
    }
    setCreating(true);
    try {
      let ok = 0;
      let fail = 0;
      const failMsgs = [];
      if (bankTaskEntries.length) {
        for (let i = 0; i < bankTaskEntries.length; i += 1) {
          const [taskId, rows] = bankTaskEntries[i];
          const task = sourceTaskMap.get(taskId) || {};
          const taskName = bankTaskEntries.length > 1 ? `${name}-${i + 1}` : name;
          const bankQuestionIds = rows.map((row) => Number(row?.bank_question_id)).filter((x) => Number.isInteger(x));
          // eslint-disable-next-line no-await-in-loop
          const res = await createJudgeTask(tenantId, {
            task_name: taskName,
            source_task_id: taskId,
            source_task_name: String(task?.task_name || '').trim(),
            bank_question_ids: bankQuestionIds,
          });
          if (res?.task?.task_id) ok += 1;
          else {
            fail += 1;
            failMsgs.push(`${taskId}: 创建失败`);
          }
        }
      } else {
        for (let i = 0; i < runIds.length; i += 1) {
          const rid = runIds[i];
          const taskName = runIds.length > 1 ? `${name}-${i + 1}` : name;
          // eslint-disable-next-line no-await-in-loop
          const res = await createJudgeTask(tenantId, { run_id: rid, task_name: taskName });
          if (res?.task?.task_id) ok += 1;
          else {
            fail += 1;
            failMsgs.push(`${rid}: 创建失败`);
          }
        }
      }
      await loadTasks(tenantId);
      setPageMode('tasks');
      form.resetFields();
      setSourceTaskBankMap({});
      if (fail === 0) {
        message.success(`已创建 ${ok} 个 Judge 任务（同城串行排队执行）`);
      } else {
        message.warning(`已创建 ${ok} 个，失败 ${fail} 个`);
        if (failMsgs.length) message.error(failMsgs.slice(0, 3).join(' ; '));
      }
      if (skippedRunIds.length) {
        message.warning(`已跳过 ${skippedRunIds.length} 个无可预览题目的 run`);
      }
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '创建 Judge 任务失败');
    } finally {
      setCreating(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {pageMode === 'tasks' && (
        <>
          <Card>
            <Space wrap>
              <Input value={taskKeyword} onChange={(e) => setTaskKeyword(e.target.value)} placeholder="任务名 / run_id" style={{ width: 280 }} />
              <Select
                value={taskStatusFilter || undefined}
                allowClear
                placeholder="任务状态"
                style={{ width: 180 }}
                onChange={(v) => setTaskStatusFilter(v || '')}
                options={[
                  { label: 'pending', value: 'pending' },
                  { label: 'running', value: 'running' },
                  { label: 'completed', value: 'completed' },
                  { label: 'failed', value: 'failed' },
                  { label: 'cancelled', value: 'cancelled' },
                ]}
              />
              <Button type="primary" onClick={() => { setQueryKeyword(taskKeyword.trim()); setQueryStatus(taskStatusFilter); }}>查询</Button>
              <Button onClick={() => { setTaskKeyword(''); setTaskStatusFilter(''); setQueryKeyword(''); setQueryStatus(''); }}>重置</Button>
              <Button onClick={() => loadAll(tenantId)}>刷新列表</Button>
              <Button type="primary" onClick={() => setPageMode('create')}>新建Judge任务</Button>
            </Space>
          </Card>
          <Card>
            <Table
              rowKey={(r) => String(r?.task_id || '')}
              columns={taskColumns}
              dataSource={filteredTasks}
              loading={loading}
              size="small"
              pagination={{ pageSize: 12, showSizeChanger: false }}
              scroll={{ x: 1500 }}
            />
          </Card>
        </>
      )}

      {pageMode === 'create' && (
        <Card title="新建 Judge 任务">
          <Typography.Paragraph type="secondary">
            同一城市任务会自动串行排队；切换城市不会清空已创建队列。
          </Typography.Paragraph>
          <Form form={form} layout="vertical" onFinish={onSubmit}>
            <Form.Item name="task_name" label="任务名称" rules={[{ required: true, whitespace: true, message: '请输入任务名称' }]}>
              <Input placeholder="例如：北京-3月批量Judge-第1批" style={{ width: 420 }} />
            </Form.Item>
            <Form.Item name="source_task_ids" label="选择出题任务（推荐，可多选）">
              <Select
                mode="multiple"
                showSearch
                optionFilterProp="label"
                style={{ width: '100%' }}
                placeholder="按 AI出题任务名选择；会直接加载正式落库题"
                options={sourceTaskOptions}
              />
            </Form.Item>
            <Form.Item name="run_ids" label="选择 run（兜底，可多选，按创建顺序串行）">
              <Select
                mode="tags"
                showSearch
                optionFilterProp="label"
                style={{ width: '100%' }}
                placeholder="仅在不走正式落库题直 Judge 时使用；可直接粘贴 run_id 回车"
                options={runOptions}
              />
            </Form.Item>
            <Typography.Paragraph type="secondary" style={{ marginTop: -8 }}>
              当前租户：{tenantId || '-'}；已加载出题任务：{sourceTaskOptions.length}；已加载可选 run：{runOptions.length}；已选正式落库题：{Object.values(sourceTaskBankMap || {}).reduce((sum, rows) => sum + rows.length, 0)}
            </Typography.Paragraph>
            {Array.isArray(selectedRunIds) && selectedRunIds.length > 0 && (
              <Card size="small" title={`已选题目预览（去重后共 ${selectedQuestions.length} 题）`} style={{ marginBottom: 12 }}>
                <Table
                  rowKey={(r) => r.key}
                  columns={selectedQuestionColumns}
                  dataSource={selectedQuestions}
                  loading={previewLoading}
                  size="small"
                  pagination={{ pageSize: 100, showSizeChanger: false }}
                  scroll={{ x: 1800 }}
                />
              </Card>
            )}
            <Space>
              <Button type="primary" htmlType="submit" loading={creating}>创建任务</Button>
              <Button onClick={() => setPageMode('tasks')}>返回列表</Button>
            </Space>
          </Form>
        </Card>
      )}
    </Space>
  );
}
