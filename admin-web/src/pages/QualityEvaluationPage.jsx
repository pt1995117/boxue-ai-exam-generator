import React, { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Card,
  Col,
  Descriptions,
  InputNumber,
  Row,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  getQaDrift,
  getQaOverview,
  getQaOpsWeekly,
  getQaPricing,
  getQaReleaseReport,
  getQaRunDetail,
  getQaThresholds,
  getQaTrends,
  listMaterials,
  listQaAlerts,
  listQaLlmCalls,
  listQaRuns,
  updateQaAlertStatus,
  updateQaPricing,
  updateQaThresholds,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const { Text } = Typography;

function pct(v) {
  const n = Number(v || 0);
  return `${(n * 100).toFixed(2)}%`;
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

export default function QualityEvaluationPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('');
  const [days, setDays] = useState(30);
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [overview, setOverview] = useState({});
  const [runDetail, setRunDetail] = useState({});
  const [llmCalls, setLlmCalls] = useState([]);
  const [trends, setTrends] = useState([]);
  const [thresholds, setThresholds] = useState({});
  const [alerts, setAlerts] = useState([]);
  const [pricing, setPricing] = useState({});
  const [opsWeekly, setOpsWeekly] = useState({});
  const [driftBase, setDriftBase] = useState('');
  const [driftTarget, setDriftTarget] = useState('');
  const [driftResult, setDriftResult] = useState(null);
  const [releaseReport, setReleaseReport] = useState(null);
  const [llmQuestionFilter, setLlmQuestionFilter] = useState('');

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadAll = async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const mats = await listMaterials(tenantId);
      const mItems = mats.items || [];
      setMaterials(mItems);
      const effective = mItems.find((x) => x.status === 'effective');
      const defaultMid = materialVersionId || (effective?.material_version_id || '');

      let effectiveMid = materialVersionId || defaultMid || '';
      let runRes = await listQaRuns(tenantId, {
        material_version_id: effectiveMid || undefined,
        days,
        page: 1,
        page_size: 200,
      });
      let runItems = runRes.items || [];
      if (!materialVersionId && effectiveMid && runItems.length === 0) {
        runRes = await listQaRuns(tenantId, {
          days,
          page: 1,
          page_size: 200,
        });
        runItems = runRes.items || [];
        if (runItems.length > 0) {
          effectiveMid = '';
          message.info('当前生效教材暂无评估数据，已切换为全部教材范围');
        }
      }
      setRuns(runItems);
      const selectedStillExists = runItems.some((x) => String(x.run_id) === String(selectedRunId || ''));
      const firstRunId = selectedStillExists ? selectedRunId : (runItems[0]?.run_id || '');
      if (!selectedRunId && firstRunId) setSelectedRunId(firstRunId);
      if (!driftBase && runItems[1]?.run_id) setDriftBase(runItems[1].run_id);
      if (!driftTarget && runItems[0]?.run_id) setDriftTarget(runItems[0].run_id);

      const [ov, tr, th, al, pr, wk] = await Promise.all([
        getQaOverview(tenantId, { material_version_id: effectiveMid || undefined, days, run_id: firstRunId || undefined }),
        getQaTrends(tenantId, { material_version_id: effectiveMid || undefined, days }),
        getQaThresholds(tenantId),
        listQaAlerts(tenantId, { page: 1, page_size: 200 }),
        getQaPricing(tenantId),
        getQaOpsWeekly(tenantId, { days: 7, run_id: firstRunId || undefined }),
      ]);
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
        setRunDetail(detail || {});
        setLlmCalls(calls.items || []);
      } else {
        setRunDetail({});
        setLlmCalls([]);
      }
    } catch (e) {
      message.error(apiErrMsg(e, '加载评估数据失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, days, materialVersionId]);

  const onRefreshCurrentRun = async (runId) => {
    if (!tenantId || !runId) return;
    setSelectedRunId(runId);
    setLoading(true);
    try {
      const [ov, detail, calls] = await Promise.all([
        getQaOverview(tenantId, { material_version_id: materialVersionId || undefined, run_id: runId }),
        getQaRunDetail(tenantId, runId),
        listQaLlmCalls(tenantId, { run_id: runId, question_id: llmQuestionFilter || undefined, page: 1, page_size: 2000 }),
      ]);
      setOverview(ov || {});
      setRunDetail(detail || {});
      setLlmCalls(calls.items || []);
    } catch (e) {
      message.error(apiErrMsg(e, '加载运行详情失败'));
    } finally {
      setLoading(false);
    }
  };

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
      const res = await getQaDrift(tenantId, { base_run_id: driftBase, target_run_id: driftTarget });
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
      const res = await getQaReleaseReport(tenantId, { base_run_id: driftBase, target_run_id: driftTarget });
      setReleaseReport(res || null);
    } catch (e) {
      message.error(apiErrMsg(e, '发布评估失败'));
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

  const questionRows = useMemo(() => runDetail?.questions || [], [runDetail]);

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

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Card>
        <Space wrap>
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
            style={{ width: 320 }}
            value={selectedRunId || undefined}
            placeholder="选择 run"
            options={runs.map((r) => ({ label: `${r.run_id} | ${r.ended_at || ''}`, value: r.run_id }))}
            onChange={onRefreshCurrentRun}
            showSearch
            optionFilterProp="label"
          />
        </Space>
      </Card>

      <Tabs
        items={[
          {
            key: 'overview',
            label: '总览',
            children: (
              <Row gutter={12}>
                {[
                  ['hard_pass_rate', '硬通过率', pct(overview.hard_pass_rate)],
                  ['quality_score_avg', '质量均分', Number(overview.quality_score_avg || 0).toFixed(2)],
                  ['risk_high_rate', '高风险率', pct(overview.risk_high_rate)],
                  ['logic_pass_rate', '逻辑合格率', pct(overview.logic_pass_rate)],
                  ['duplicate_rate', '重复率', pct(overview.duplicate_rate)],
                  ['knowledge_match_rate', '考点命中率', pct(overview.knowledge_match_rate)],
                  ['avg_tokens_per_question', '平均Token/题', Number(overview.avg_tokens_per_question || 0).toFixed(2)],
                  ['avg_latency_ms_per_question', '平均时长ms/题', Number(overview.avg_latency_ms_per_question || 0).toFixed(2)],
                  ['avg_cost_per_question', '平均成本/题', `${Number(overview.avg_cost_per_question || 0).toFixed(4)} ${overview.currency || 'CNY'}`],
                  ['total_cost', '总成本', `${Number(overview.total_cost || 0).toFixed(4)} ${overview.currency || 'CNY'}`],
                ].map((x) => (
                  <Col xs={24} md={12} lg={6} key={x[0]} style={{ marginBottom: 12 }}>
                    <Card loading={loading}>
                      <Text type="secondary">{x[1]}</Text>
                      <div style={{ fontSize: 24, fontWeight: 600 }}>{x[2]}</div>
                    </Card>
                  </Col>
                ))}
              </Row>
            ),
          },
          {
            key: 'cost_board',
            label: '金额成本',
            children: (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Card size="small" title="成本概览">
                  <Descriptions size="small" bordered column={2}>
                    <Descriptions.Item label="currency">{overview.currency || 'CNY'}</Descriptions.Item>
                    <Descriptions.Item label="avg_cost_per_question">{Number(overview.avg_cost_per_question || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="total_cost">{Number(overview.total_cost || 0).toFixed(6)}</Descriptions.Item>
                    <Descriptions.Item label="avg_cost_per_call">{Number((runDetail?.batch_metrics || {}).avg_cost_per_call || 0).toFixed(6)}</Descriptions.Item>
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
                      <Descriptions.Item label="currency">{pricing.currency || 'CNY'}</Descriptions.Item>
                      <Descriptions.Item label="default_prompt_per_1k">{Number(pricing.default_prompt_per_1k || 0).toFixed(6)}</Descriptions.Item>
                      <Descriptions.Item label="default_completion_per_1k">{Number(pricing.default_completion_per_1k || 0).toFixed(6)}</Descriptions.Item>
                    </Descriptions>
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
                    ['total_calls', '总调用数', healthCards.totalCalls],
                    ['error_calls', '错误调用数', healthCards.errCalls],
                    ['success_rate', '调用成功率', pct(healthCards.successRate)],
                    ['p95_latency', 'P95时延(ms)', Number(healthCards.p95 || 0).toFixed(2)],
                    ['avg_latency', '平均时延(ms)', Number(healthCards.avgLatency || 0).toFixed(2)],
                    ['total_tokens', '总Token', healthCards.totalTokens],
                    ['avg_tokens_call', '平均Token/调用', Number(healthCards.avgTokensPerCall || 0).toFixed(2)],
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
              </Space>
            ),
          },
          {
            key: 'question',
            label: '单题评估',
            children: (
              <Table
                size="small"
                loading={loading}
                rowKey={(r) => r.question_id || `${r.index}`}
                dataSource={questionRows}
                pagination={{ pageSize: 20 }}
                columns={[
                  { title: '题号', dataIndex: 'index', width: 70 },
                  { title: 'question_id', dataIndex: 'question_id', width: 220, ellipsis: true },
                  { title: '硬通过', render: (_, r) => (r.hard_gate?.pass ? <Tag color="green">PASS</Tag> : <Tag color="red">FAIL</Tag>) },
                  { title: '逻辑分', render: (_, r) => r.quality?.logic_score ?? 0, width: 90 },
                  { title: '干扰项', render: (_, r) => r.quality?.distractor_score ?? 0, width: 90 },
                  { title: '考点匹配', render: (_, r) => r.quality?.knowledge_match_score ?? 0, width: 100 },
                  { title: '风险', render: (_, r) => <Tag color={r.risk?.level === 'high' ? 'red' : r.risk?.level === 'medium' ? 'orange' : 'green'}>{r.risk?.level || 'low'}</Tag>, width: 100 },
                  { title: 'critic loops', render: (_, r) => r.stability?.critic_loops ?? 0, width: 100 },
                  { title: 'LLM calls', render: (_, r) => r.stability?.llm_calls ?? 0, width: 100 },
                  { title: 'tokens', render: (_, r) => r.stability?.tokens ?? 0, width: 100 },
                  { title: 'latency(ms)', render: (_, r) => r.stability?.latency_ms ?? 0, width: 110 },
                ]}
                expandable={{
                  expandedRowRender: (r) => (
                    <Descriptions size="small" column={1} bordered>
                      <Descriptions.Item label="题干">{r.question_text || '-'}</Descriptions.Item>
                      <Descriptions.Item label="failed_rules">{(r.hard_gate?.failed_rules || []).join(' | ') || '-'}</Descriptions.Item>
                      <Descriptions.Item label="quality_issues">{(r.issues?.quality_issues || []).join(' | ') || '-'}</Descriptions.Item>
                      <Descriptions.Item label="missing_conditions">{(r.issues?.missing_conditions || []).join(' | ') || '-'}</Descriptions.Item>
                      <Descriptions.Item label="all_issues">{(r.issues?.all_issues || []).join(' | ') || '-'}</Descriptions.Item>
                      <Descriptions.Item label="fix_strategy">{r.issues?.fix_strategy || '-'}</Descriptions.Item>
                    </Descriptions>
                  ),
                }}
              />
            ),
          },
          {
            key: 'llm_calls',
            label: '模型调用明细',
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
              <Card loading={loading}>
                <Descriptions size="small" bordered column={2}>
                  {Object.entries(runDetail?.batch_metrics || {}).map(([k, v]) => (
                    <Descriptions.Item key={k} label={k}>{typeof v === 'number' ? Number(v).toFixed(4) : String(v)}</Descriptions.Item>
                  ))}
                </Descriptions>
              </Card>
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
                  { title: 'hard_pass_rate', dataIndex: 'hard_pass_rate' },
                  { title: 'quality_score_avg', dataIndex: 'quality_score_avg' },
                  { title: 'risk_high_rate', dataIndex: 'risk_high_rate' },
                  { title: 'logic_pass_rate', dataIndex: 'logic_pass_rate' },
                  { title: 'avg_tokens_per_question', dataIndex: 'avg_tokens_per_question' },
                  { title: 'avg_latency_ms_per_question', dataIndex: 'avg_latency_ms_per_question' },
                  { title: 'avg_cost_per_question', dataIndex: 'avg_cost_per_question' },
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
                <Space wrap>
                  <Select style={{ width: 360 }} placeholder="基线 run" value={driftBase || undefined} onChange={setDriftBase} options={runs.map((r) => ({ label: `${r.run_id} | ${r.ended_at || ''}`, value: r.run_id }))} />
                  <Select style={{ width: 360 }} placeholder="目标 run" value={driftTarget || undefined} onChange={setDriftTarget} options={runs.map((r) => ({ label: `${r.run_id} | ${r.ended_at || ''}`, value: r.run_id }))} />
                  <Button onClick={onRunDrift}>执行对比</Button>
                </Space>
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
                <Space wrap>
                  <Select style={{ width: 360 }} placeholder="基线 run" value={driftBase || undefined} onChange={setDriftBase} options={runs.map((r) => ({ label: `${r.run_id} | ${r.ended_at || ''}`, value: r.run_id }))} />
                  <Select style={{ width: 360 }} placeholder="目标 run" value={driftTarget || undefined} onChange={setDriftTarget} options={runs.map((r) => ({ label: `${r.run_id} | ${r.ended_at || ''}`, value: r.run_id }))} />
                  <Button onClick={onRunReleaseReport}>生成发布结论</Button>
                </Space>
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
