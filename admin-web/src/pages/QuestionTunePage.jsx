import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Descriptions, Input as AntInput, Row, Skeleton, Space, Tag, Typography, message } from 'antd';
import { ArrowLeftOutlined, ReloadOutlined, RobotOutlined, SaveOutlined } from '@ant-design/icons';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { getBankQuestion, optimizeBankQuestion, updateBankQuestion } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const { TextArea } = AntInput;

const OPTION_KEYS = ['选项1', '选项2', '选项3', '选项4', '选项5', '选项6', '选项7', '选项8'];
const EDITABLE_FIELDS = ['题干', ...OPTION_KEYS, '正确答案', '解析', '难度值'];
const READONLY_FIELD_LABELS = {
  question_id: '题目ID',
  question_type: '题型',
  题型: '题型',
  source_task_name: '出题任务',
  source_task_id: '出题任务ID',
  source_run_id: '来源运行ID',
  来源路径: '来源切片',
  关联切片路径: '关联切片路径',
  关联切片路径文本: '关联切片路径文本',
  切片原文: '来源切片原文',
  来源切片原文: '来源切片原文',
  全部切片原文: '全部切片原文',
  关联切片原文: '关联切片原文',
  一级知识点: '一级知识点',
  二级知识点: '二级知识点',
  三级知识点: '三级知识点',
  一级知识点路径: '一级知识点路径',
  二级知识点路径: '二级知识点路径',
  三级知识点路径: '三级知识点路径',
  知识点: '知识点',
  知识点路径: '知识点路径',
  参考母题全文: '关联母题',
  mother_questions_full_text: '关联母题',
  母题题干: '关联母题',
  母题: '关联母题',
};

const READONLY_CANONICAL_KEYS = {
  question_type: '题型',
  题目类型: '题型',
  切片原文: '来源切片原文',
  关联切片路径文本: '关联切片路径',
  参考母题全文: '关联母题',
  mother_questions_full_text: '关联母题',
  母题题干: '关联母题',
  母题: '关联母题',
};

const TRAILING_READONLY_KEYS = new Set([
  'question_id',
  '题型',
  'source_task_name',
  'source_task_id',
  'source_run_id',
  '一级知识点',
  '二级知识点',
  '三级知识点',
  '一级知识点路径',
  '二级知识点路径',
  '三级知识点路径',
  '知识点',
  '知识点路径',
]);

function buildTuneForm(record) {
  const next = {};
  EDITABLE_FIELDS.forEach((key) => {
    next[key] = record?.[key] ?? '';
  });
  return next;
}

function buildEditableTuneItem(formValue, visibleOptionKeys) {
  const visibleOptionKeySet = new Set(visibleOptionKeys || []);
  const out = {};
  EDITABLE_FIELDS.forEach((key) => {
    const raw = formValue?.[key];
    const value = typeof raw === 'string' ? raw : (raw ?? '');
    if (OPTION_KEYS.includes(key) && !visibleOptionKeySet.has(key)) {
      out[key] = '';
      return;
    }
    out[key] = value;
  });
  return out;
}

function getChangedEditableFields(before, after) {
  const beforeItem = before || {};
  const afterItem = after || {};
  return EDITABLE_FIELDS
    .map((key) => {
      const beforeText = String(beforeItem?.[key] ?? '').trim();
      const afterText = String(afterItem?.[key] ?? '').trim();
      if (beforeText === afterText) return null;
      return { key, before: beforeText, after: afterText };
    })
    .filter(Boolean);
}

function getQuestionType(record) {
  return String(record?.question_type || record?.题型 || record?.题目类型 || '').trim();
}

function getVisibleOptionKeys(record) {
  const questionType = getQuestionType(record);
  if (questionType === '判断题') return OPTION_KEYS.slice(0, 2);
  if (questionType === '单选题' || questionType === '多选题') return OPTION_KEYS.slice(0, 4);
  return OPTION_KEYS.slice(0, 4);
}

function getReadonlyTuneEntries(record) {
  const item = record?.preview_context && typeof record.preview_context === 'object'
    ? record.preview_context
    : (record && typeof record === 'object' ? record : {});
  const deduped = [];
  const seenCanonicalKeys = new Set();
  const entries = Object.entries(item).filter(([key]) => {
    if (EDITABLE_FIELDS.includes(key)) return false;
    return (
      READONLY_FIELD_LABELS[key]
      || key.includes('切片')
      || key.includes('知识点')
      || key.includes('母题')
      || key.includes('source_')
      || key.includes('reference_')
    );
  });
  entries.forEach(([rawKey, value]) => {
    const key = READONLY_CANONICAL_KEYS[rawKey] || rawKey;
    if (seenCanonicalKeys.has(key)) return;
    seenCanonicalKeys.add(key);
    deduped.push([key, value]);
  });
  return deduped.sort(([keyA], [keyB]) => {
    const aTrailing = TRAILING_READONLY_KEYS.has(keyA);
    const bTrailing = TRAILING_READONLY_KEYS.has(keyB);
    if (aTrailing === bTrailing) return 0;
    return aTrailing ? 1 : -1;
  });
}

function renderReadonlyTuneValue(value) {
  if (value === null || value === undefined || value === '') return '（空）';
  if (Array.isArray(value)) {
    return value.length ? value.map((item, index) => `${index + 1}. ${renderReadonlyTuneValue(item)}`).join('\n') : '（空）';
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2);
    } catch (_) {
      return String(value);
    }
  }
  return String(value);
}

function normalizeTuneDirections(record) {
  const rows = Array.isArray(record?.tune_directions) ? record.tune_directions : [];
  return rows
    .map((row, index) => {
      if (typeof row === 'string') {
        const text = row.trim();
        return text ? { source: '系统', severity: 'warning', title: `调优方向 ${index + 1}`, detail: text, direction: '' } : null;
      }
      if (!row || typeof row !== 'object') return null;
      const title = String(row.title || '').trim();
      const detail = String(row.detail || '').trim();
      const direction = String(row.direction || '').trim();
      if (!title && !detail && !direction) return null;
      return {
        source: String(row.source || '系统').trim(),
        severity: String(row.severity || 'warning').trim(),
        title,
        detail,
        direction,
      };
    })
    .filter(Boolean);
}

function tuneDirectionsToFeedback(rows) {
  return (rows || [])
    .map((row, index) => {
      const source = row.source ? `【${row.source}】` : '';
      const title = row.title || `问题 ${index + 1}`;
      const parts = [`${index + 1}. ${source}${title}`];
      if (row.detail) parts.push(`问题依据：${row.detail}`);
      if (row.direction) parts.push(`调优方向：${row.direction}`);
      return parts.join('\n');
    })
    .join('\n\n');
}

function tuneDirectionColor(row) {
  const severity = String(row?.severity || '').toLowerCase();
  if (severity === 'error') return 'red';
  if (severity === 'success') return 'green';
  const source = String(row?.source || '').toLowerCase();
  if (source.includes('judge')) return 'orange';
  if (source.includes('critic')) return 'purple';
  return 'blue';
}

function groupTuneDirections(rows) {
  const grouped = { Judge: [], Critic: [], 其他: [] };
  (rows || []).forEach((row) => {
    const source = String(row?.source || '').toLowerCase();
    if (source.includes('judge')) {
      grouped.Judge.push(row);
    } else if (source.includes('critic')) {
      grouped.Critic.push(row);
    } else {
      grouped.其他.push(row);
    }
  });
  return grouped;
}

export default function QuestionTunePage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { questionId } = useParams();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(true);
  const [question, setQuestion] = useState(location.state?.question || null);
  const [entrySnapshot, setEntrySnapshot] = useState(location.state?.question || null);
  const [form, setForm] = useState(buildTuneForm(location.state?.question || {}));
  const [feedback, setFeedback] = useState('');
  const [optimizing, setOptimizing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [latestOptimizeEventId, setLatestOptimizeEventId] = useState('');
  const [latestOptimizeBefore, setLatestOptimizeBefore] = useState(null);
  const [latestOptimizeAfter, setLatestOptimizeAfter] = useState(null);
  const [optimizeAttemptCount, setOptimizeAttemptCount] = useState(0);
  const [optimizeError, setOptimizeError] = useState('');

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    let active = true;
    async function loadQuestion() {
      if (!tenantId || questionId == null) return;
      setLoading(true);
      try {
        const res = await getBankQuestion(tenantId, questionId);
        if (!active) return;
        const item = res?.item || {};
        setQuestion(item);
        setEntrySnapshot(item);
        setForm(buildTuneForm(item));
        setFeedback('');
        setLatestOptimizeEventId('');
        setLatestOptimizeBefore(null);
        setLatestOptimizeAfter(null);
        setOptimizeAttemptCount(0);
        setOptimizeError('');
      } catch (e) {
        if (!active) return;
        message.error(e?.response?.data?.error?.message || '加载题目失败');
      } finally {
        if (active) setLoading(false);
      }
    }
    loadQuestion();
    return () => {
      active = false;
    };
  }, [tenantId, questionId]);

  const visibleOptionKeys = useMemo(() => getVisibleOptionKeys(question), [question]);
  const readonlyEntries = useMemo(() => getReadonlyTuneEntries(question), [question]);
  const tuneDirections = useMemo(() => normalizeTuneDirections(question), [question]);
  const groupedTuneDirections = useMemo(() => groupTuneDirections(tuneDirections), [tuneDirections]);
  const latestOptimizeChanges = useMemo(
    () => getChangedEditableFields(latestOptimizeBefore, latestOptimizeAfter),
    [latestOptimizeBefore, latestOptimizeAfter]
  );

  const buildTuneItem = () => {
    const base = question && typeof question === 'object' ? { ...question } : {};
    return { ...base, ...buildEditableTuneItem(form, visibleOptionKeys) };
  };

  const onReset = () => {
    setForm(buildTuneForm(entrySnapshot || {}));
    setFeedback('');
    setLatestOptimizeEventId('');
    setLatestOptimizeBefore(null);
    setLatestOptimizeAfter(null);
    setOptimizeAttemptCount(0);
    setOptimizeError('');
  };

  const onAiOptimize = async () => {
    if (!question) return;
    const nextFeedback = String(feedback || '').trim();
    if (!nextFeedback && !tuneDirections.length) {
      message.warning('请先输入反馈意见');
      return;
    }
    const feedbackForOptimize = nextFeedback || '请按系统识别的 Judge / Critic 调优方向进行调优。';
    const draft = buildEditableTuneItem(form, visibleOptionKeys);
    const beforeSnapshot = buildEditableTuneItem(form, visibleOptionKeys);
    setOptimizing(true);
    setOptimizeAttemptCount((prev) => prev + 1);
    setOptimizeError('');
    try {
      const res = await optimizeBankQuestion(tenantId, {
        question_id: question.question_id,
        feedback: feedbackForOptimize,
        question: draft,
      });
      const nextItem = { ...question, ...(res?.item || {}), question_id: question.question_id };
      const afterSnapshot = buildEditableTuneItem(buildTuneForm(nextItem), getVisibleOptionKeys(nextItem));
      setQuestion(nextItem);
      setForm(buildTuneForm(nextItem));
      setLatestOptimizeBefore(beforeSnapshot);
      setLatestOptimizeAfter(afterSnapshot);
      setLatestOptimizeEventId(String(res?.optimize_event_id || '').trim());
      setOptimizeError('');
      message.success('AI 调优已完成，请继续确认后保存');
    } catch (e) {
      const apiMessage = String(e?.response?.data?.error?.message || '').trim();
      const timeoutMessage = e?.code === 'ECONNABORTED'
        ? 'AI 调优请求超时，模型可能仍在上游排队或重试。'
        : '';
      const nextError = apiMessage || timeoutMessage || 'AI 调优失败';
      setOptimizeError(nextError);
      message.error(nextError);
    } finally {
      setOptimizing(false);
    }
  };

  const onSave = async () => {
    if (!question) return;
    const item = buildTuneItem();
    if (!String(item?.题干 || '').trim()) {
      message.warning('题干不能为空');
      return;
    }
    setSaving(true);
    try {
      const res = await updateBankQuestion(tenantId, {
        question_id: question.question_id,
        item,
        optimize_event_id: latestOptimizeEventId || undefined,
      });
      const savedItem = { ...question, ...(res?.item || item), question_id: question.question_id };
      setQuestion(savedItem);
      setForm(buildTuneForm(savedItem));
      setLatestOptimizeEventId('');
      setLatestOptimizeBefore(null);
      setLatestOptimizeAfter(null);
      setOptimizeError('');
      message.success('题目已保存到题库');
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const questionType = getQuestionType(question);
  const stemLength = String(form?.题干 || '').trim().length;
  const explanationLength = String(form?.解析 || '').trim().length;

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        styles={{ body: { padding: 20 } }}
        style={{
          borderRadius: 20,
          background: 'linear-gradient(135deg, #fff9ef 0%, #ffffff 58%, #f4f8ff 100%)',
          border: '1px solid #f0dfbf',
          boxShadow: '0 20px 45px rgba(30, 60, 90, 0.08)',
        }}
      >
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
            <Space wrap size={10}>
              <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/question-bank')}>
                返回题库
              </Button>
              <Tag color="blue">{questionType || '未标注题型'}</Tag>
              <Tag>{`题目ID ${questionId}`}</Tag>
              {question?.source_task_name ? <Tag color="gold">{question.source_task_name}</Tag> : null}
            </Space>
            <Space wrap>
              <Button icon={<ReloadOutlined />} onClick={onReset} disabled={loading || optimizing || saving}>
                重置
              </Button>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                onClick={onSave}
                loading={saving}
                disabled={loading || optimizing}
              >
                保存最终版本
              </Button>
            </Space>
          </Space>
          <div>
            <Typography.Title level={3} style={{ margin: 0, fontFamily: '"Noto Serif SC", "Songti SC", serif' }}>
              单题调优工作台
            </Typography.Title>
            <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0 0' }}>
              左侧只放老师会改的题面内容，右侧集中展示 AI 调优和只读依据。页面拉满展示，避免弹窗里看不全。
            </Typography.Paragraph>
          </div>
          <Space wrap size={[8, 8]}>
            <Tag bordered={false} color="processing">{`题干 ${stemLength} 字`}</Tag>
            <Tag bordered={false} color="purple">{`解析 ${explanationLength} 字`}</Tag>
            <Tag bordered={false} color="cyan">
              {questionType === '判断题' ? '判断题固定 2 个选项' : '单选/多选固定 4 个选项'}
            </Tag>
            <Tag bordered={false} color={editing ? 'success' : 'default'}>
              {editing ? '手动修改区可编辑' : '手动修改区已锁定'}
            </Tag>
          </Space>
        </Space>
      </Card>

      {loading ? (
        <Card style={{ borderRadius: 20 }}>
          <Skeleton active paragraph={{ rows: 10 }} />
        </Card>
      ) : !question ? (
        <Alert
          type="error"
          showIcon
          message="题目加载失败"
          description="当前无法获取题目内容，请返回题库页后重新进入。"
        />
      ) : (
        <Row gutter={[16, 16]} align="top">
          <Col xs={24} xl={15}>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Card
                title="AI 调优区"
                extra={<Typography.Text type="secondary">主流程：先写反馈，再让 AI 调优</Typography.Text>}
                style={{ borderRadius: 20, overflow: 'hidden' }}
                styles={{ header: { background: '#f6f9ff' } }}
              >
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  {tuneDirections.length ? (
                    <div
                      style={{
                        border: '1px solid #e5e7eb',
                        borderRadius: 8,
                        padding: 12,
                        background: '#fff',
                      }}
                    >
                      <Space wrap style={{ width: '100%', justifyContent: 'space-between', marginBottom: 10 }}>
                        <Typography.Text strong>Judge / Critic 调优方向</Typography.Text>
                        <Typography.Text type="secondary">只读依据，不会同步进题干、选项或解析</Typography.Text>
                      </Space>
                      {['Judge', 'Critic', '其他'].map((groupName) => {
                        const rows = groupedTuneDirections[groupName] || [];
                        if (!rows.length) return null;
                        return (
                          <div key={groupName} style={{ marginTop: groupName === 'Judge' ? 0 : 12 }}>
                            <Space size={6} style={{ marginBottom: 6 }}>
                              <Tag color={groupName === 'Judge' ? 'orange' : groupName === 'Critic' ? 'purple' : 'blue'}>{groupName}</Tag>
                              <Typography.Text type="secondary">{`${rows.length} 条`}</Typography.Text>
                            </Space>
                            <Space direction="vertical" size={10} style={{ width: '100%' }}>
                              {rows.map((row, index) => (
                                <div key={`${groupName}-${row.title}-${index}`}>
                                  <Typography.Text strong>{row.title || `问题 ${index + 1}`}</Typography.Text>
                                  {row.detail ? (
                                    <Typography.Paragraph style={{ margin: '6px 0 0 0', whiteSpace: 'pre-wrap' }}>
                                      {row.detail}
                                    </Typography.Paragraph>
                                  ) : null}
                                  {row.direction ? (
                                    <Typography.Paragraph type="secondary" style={{ margin: '4px 0 0 0', whiteSpace: 'pre-wrap' }}>
                                      {`建议：${row.direction}`}
                                    </Typography.Paragraph>
                                  ) : null}
                                </div>
                              ))}
                            </Space>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <Alert
                      type="success"
                      showIcon
                      message="当前题目没有可识别的 Judge 或 Critic 调优方向；仍可输入人工反馈后调优。"
                    />
                  )}
                  <Alert
                    type="info"
                    showIcon
                    message="AI 会基于当前最终版本、上方调优方向和下方反馈重写题面；调优方向只作为依据，不会作为题目字段保存。"
                  />
                  {optimizeError ? (
                    <Alert
                      type="error"
                      showIcon
                      message={`最近一次 AI 调优失败（第 ${optimizeAttemptCount} 次）`}
                      description={(
                        <Space direction="vertical" size={4}>
                          <Typography.Text>{optimizeError}</Typography.Text>
                          <Typography.Text type="secondary">
                            当前右侧最终题目不会被失败结果覆盖。可直接再次点击“AI 按反馈调优”重试，或切到手动修改后继续保存。
                          </Typography.Text>
                        </Space>
                      )}
                    />
                  ) : null}
                  <div>
                    <Typography.Text strong>反馈意见</Typography.Text>
                    <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                      人工补充，可留空
                    </Typography.Text>
                    <TextArea
                      value={feedback}
                      onChange={(e) => setFeedback(e.target.value)}
                      rows={8}
                      placeholder="可选。示例：题干过长，请缩短并改成更贴近门店场景；干扰项要更像真实业务说法。"
                      style={{ marginTop: 8 }}
                    />
                  </div>
                  <Button
                    type="primary"
                    icon={<RobotOutlined />}
                    onClick={onAiOptimize}
                    loading={optimizing}
                    disabled={loading || saving}
                    block
                  >
                    {optimizeAttemptCount > 0 ? '重新尝试 AI 调优' : 'AI 按反馈调优'}
                  </Button>
                  {latestOptimizeAfter ? (
                    <div
                      style={{
                        border: '1px solid #d9f7be',
                        borderRadius: 8,
                        padding: 12,
                        background: '#fcfffa',
                      }}
                    >
                      <Space wrap style={{ width: '100%', justifyContent: 'space-between', marginBottom: 10 }}>
                        <Typography.Text strong>本次 AI 调优结果对比</Typography.Text>
                        <Typography.Text type="secondary">{latestOptimizeChanges.length ? `${latestOptimizeChanges.length} 处变化` : '无题面变化'}</Typography.Text>
                      </Space>
                      {latestOptimizeChanges.length ? (
                        <Space direction="vertical" size={10} style={{ width: '100%' }}>
                          {latestOptimizeChanges.map((row) => (
                            <div key={row.key}>
                              <Tag color="green">{row.key}</Tag>
                              <Row gutter={8} style={{ marginTop: 6 }}>
                                <Col xs={24} md={12}>
                                  <Typography.Text type="secondary">调优前</Typography.Text>
                                  <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                    {row.before || '（空）'}
                                  </Typography.Paragraph>
                                </Col>
                                <Col xs={24} md={12}>
                                  <Typography.Text type="secondary">调优后</Typography.Text>
                                  <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                                    {row.after || '（空）'}
                                  </Typography.Paragraph>
                                </Col>
                              </Row>
                            </div>
                          ))}
                        </Space>
                      ) : (
                        <Alert type="warning" showIcon message="AI 返回结果没有题面变化，请补充更明确的反馈后重试。" />
                      )}
                    </div>
                  ) : null}
                </Space>
              </Card>

              <Card
                title="题目来源与知识点"
                extra={<Typography.Text type="secondary">只读，不参与编辑</Typography.Text>}
                style={{ borderRadius: 20, overflow: 'hidden' }}
                styles={{ header: { background: '#f8f8f8' } }}
              >
                <Descriptions size="small" bordered column={1}>
                  {readonlyEntries.map(([key, value]) => (
                    <Descriptions.Item key={key} label={READONLY_FIELD_LABELS[key] || key}>
                      <Typography.Text style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                        {renderReadonlyTuneValue(value)}
                      </Typography.Text>
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              </Card>
            </Space>
          </Col>

          <Col xs={24} xl={9}>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <Card
                title="最终题目"
                extra={(
                  <Space size={12}>
                    <Typography.Text type="secondary">只放最终题面</Typography.Text>
                    <Button size="small" onClick={() => setEditing((prev) => !prev)} disabled={loading || optimizing || saving}>
                      {editing ? '退出手动修改' : '手动修改'}
                    </Button>
                  </Space>
                )}
                style={{ borderRadius: 20, overflow: 'hidden' }}
                styles={{ header: { background: '#fff8ed' } }}
              >
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  {!editing ? (
                    <Alert
                      type="info"
                      showIcon
                      message="这里始终展示当前最终版本。AI 调优后的结果会自动同步到这里；如需人工补改，请点击右上角“手动修改”。"
                    />
                  ) : (
                    <Alert
                      type="success"
                      showIcon
                      message="当前可直接手动微调题面；保存时会以这里的内容作为最终版本写回题库。"
                    />
                  )}
                  <div>
                    <Typography.Text strong>题干</Typography.Text>
                    <TextArea
                      value={form['题干'] || ''}
                      onChange={(e) => setForm((prev) => ({ ...prev, 题干: e.target.value }))}
                      rows={7}
                      placeholder="请输入题干"
                      disabled={!editing}
                      style={{ marginTop: 8 }}
                    />
                  </div>
                  <div>
                    <Space size={8} style={{ marginBottom: 8 }}>
                      <Typography.Text strong>选项</Typography.Text>
                      <Typography.Text type="secondary">
                        {questionType === '判断题' ? '当前显示 2 个选项' : '当前显示 4 个选项'}
                      </Typography.Text>
                    </Space>
                    <Space direction="vertical" size={10} style={{ width: '100%' }}>
                      {visibleOptionKeys.map((key, index) => (
                        <AntInput
                          key={key}
                          value={form[key] || ''}
                          onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
                          addonBefore={String.fromCharCode(65 + index)}
                          placeholder={`请输入选项 ${String.fromCharCode(65 + index)}`}
                          disabled={!editing}
                        />
                      ))}
                    </Space>
                  </div>
                  <Row gutter={12}>
                    <Col xs={24} md={12}>
                      <Typography.Text strong>正确答案</Typography.Text>
                      <AntInput
                        value={form['正确答案'] || ''}
                        onChange={(e) => setForm((prev) => ({ ...prev, 正确答案: e.target.value }))}
                        placeholder={questionType === '多选题' ? '如 AC' : '如 A'}
                        disabled={!editing}
                        style={{ marginTop: 8 }}
                      />
                    </Col>
                    <Col xs={24} md={12}>
                      <Typography.Text strong>难度值</Typography.Text>
                      <AntInput
                        value={form['难度值'] || ''}
                        onChange={(e) => setForm((prev) => ({ ...prev, 难度值: e.target.value }))}
                        placeholder="如 0.60"
                        disabled={!editing}
                        style={{ marginTop: 8 }}
                      />
                    </Col>
                  </Row>
                  <div>
                    <Typography.Text strong>解析</Typography.Text>
                    <TextArea
                      value={form['解析'] || ''}
                      onChange={(e) => setForm((prev) => ({ ...prev, 解析: e.target.value }))}
                      rows={10}
                      placeholder="请输入解析"
                      disabled={!editing}
                      style={{ marginTop: 8 }}
                    />
                  </div>
                </Space>
              </Card>
            </Space>
          </Col>
        </Row>
      )}
    </Space>
  );
}
