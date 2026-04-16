import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Card, Descriptions, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { cancelJudgeTask, getJudgeTask, getQaRunDetail } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const isActive = (status) => ['pending', 'running'].includes(String(status || '').toLowerCase());
const { Text, Paragraph } = Typography;
const JUDGE_INPUT_FEN_PER_KTOK = 1.23;
const JUDGE_OUTPUT_FEN_PER_KTOK = 9.8;

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

function extractStem(q) {
  return String(
    q?.题干
    || q?.question_text
    || q?.final_json?.题干
    || q?.judge_input?.stem
    || q?.offline_judge?._qa_trace?.judge_input?.stem
    || ''
  );
}

function extractOptions(q) {
  const fromArray = q?.judge_input?.options || q?.offline_judge?._qa_trace?.judge_input?.options || [];
  const arr = Array.isArray(fromArray) ? fromArray.filter((x) => String(x || '').trim()) : [];
  if (arr.length) return arr;
  const fj = q?.final_json || {};
  const keys = ['选项1', '选项2', '选项3', '选项4', '选项5', '选项6', '选项7', '选项8'];
  return keys.map((k) => String(fj?.[k] || '').trim()).filter(Boolean);
}

function extractAnswer(q) {
  return String(
    q?.answer
    || q?.正确答案
    || q?.final_json?.正确答案
    || q?.judge_input?.correct_answer
    || q?.offline_judge?._qa_trace?.judge_input?.correct_answer
    || ''
  );
}

function extractExplanation(q) {
  return String(
    q?.解析
    || q?.final_json?.解析
    || q?.judge_input?.explanation
    || q?.offline_judge?._qa_trace?.judge_input?.explanation
    || q?.issues?.reason
    || ''
  );
}

function extractTextbookSlice(q) {
  const norm = (v) => String(v || '').trim();
  const candidates = [
    q?.textbook_slice,
    q?.judge_input?.textbook_slice,
    q?.offline_judge?._qa_trace?.judge_input?.textbook_slice,
    q?.slice_text,
    q?.source_slice,
    q?.final_json?.教材原文,
    q?.final_json?.切片原文,
    q?.final_json?.知识切片,
  ].map(norm).filter(Boolean);

  const firstReal = candidates.find((x) => x && x !== '(无切片原文)' && x !== '无');
  if (firstReal) return firstReal;

  const related = [
    ...(Array.isArray(q?.related_slices) ? q.related_slices : []),
    ...(Array.isArray(q?.reference_slices) ? q.reference_slices : []),
    ...(Array.isArray(q?.judge_input?.related_slices) ? q.judge_input.related_slices : []),
    ...(Array.isArray(q?.judge_input?.reference_slices) ? q.judge_input.reference_slices : []),
    ...(Array.isArray(q?.offline_judge?._qa_trace?.judge_input?.related_slices)
      ? q.offline_judge._qa_trace.judge_input.related_slices
      : []),
    ...(Array.isArray(q?.offline_judge?._qa_trace?.judge_input?.reference_slices)
      ? q.offline_judge._qa_trace.judge_input.reference_slices
      : []),
  ]
    .map(norm)
    .filter(Boolean);
  const uniqRelated = Array.from(new Set(related));
  if (uniqRelated.length) return uniqRelated.join('\n');

  return candidates[0] || '';
}

function extractQualityConclusion(oj) {
  if (!oj || typeof oj !== 'object') return '';
  const basis = String(oj.quality_scoring_basis || '').trim();
  const qualityReasons = Array.isArray(oj.quality_reasons)
    ? oj.quality_reasons.map((x) => String(x || '').trim()).filter(Boolean)
    : [];
  const dimFeedback = (oj.quality_dimension_feedback && typeof oj.quality_dimension_feedback === 'object')
    ? Object.entries(oj.quality_dimension_feedback)
      .map(([k, v]) => `${String(k)}: ${String(v || '').trim()}`)
      .filter((x) => x && !x.endsWith(':'))
    : [];
  const parts = [];
  if (basis) parts.push(`核心依据：${basis}`);
  if (qualityReasons.length) parts.push(`质量原因：${qualityReasons.join('；')}`);
  if (dimFeedback.length) parts.push(`分维反馈：${dimFeedback.join('；')}`);
  return parts.join('\n');
}

function extractBaselineConclusion(oj) {
  if (!oj || typeof oj !== 'object') return '';
  const parts = [];
  const dim = (oj.dimension_results && typeof oj.dimension_results === 'object') ? oj.dimension_results : {};
  const dimLines = [];
  Object.entries(dim).forEach(([name, raw]) => {
    const dr = (raw && typeof raw === 'object') ? raw : {};
    const score = Number(dr?.score_10);
    const scoreTxt = Number.isFinite(score) ? score.toFixed(1) : '-';
    const issues = Array.isArray(dr?.issues) ? dr.issues.map((x) => String(x || '').trim()).filter(Boolean) : [];
    const reasons = Array.isArray(dr?.reasons) ? dr.reasons.map((x) => String(x || '').trim()).filter(Boolean) : [];
    const status = String(dr?.status || '').toUpperCase();
    const hasSignal = issues.length > 0 || reasons.length > 0 || (Number.isFinite(score) && score < 10);
    if (!hasSignal) return;
    const issueTxt = issues.slice(0, 2).join('；');
    const reasonTxt = reasons.slice(0, 2).join('；');
    const detail = [issueTxt, reasonTxt].filter(Boolean).join('；');
    dimLines.push(`${name}(status=${status || '-'}, score=${scoreTxt})${detail ? `：${detail}` : ''}`);
  });
  if (dimLines.length) parts.push(`维度依据：${dimLines.join('\n')}`);

  const allReasons = Array.isArray(oj?.reasons) ? oj.reasons.map((x) => String(x || '').trim()).filter(Boolean) : [];
  const baselineReasons = allReasons.filter((x) => !x.startsWith('【质量评分】') && !x.startsWith('【质量评分依据】'));
  if (baselineReasons.length) parts.push(`基线原因：${baselineReasons.join('；')}`);

  const hardGate = (oj.hard_gate && typeof oj.hard_gate === 'object') ? oj.hard_gate : {};
  const hardIssues = Object.entries(hardGate)
    .filter(([, v]) => v === false)
    .map(([k]) => String(k));
  if (hardIssues.length) parts.push(`硬闸门未通过：${hardIssues.join('、')}`);
  return parts.join('\n');
}

function diffMs(startAt, endAt) {
  const s = Date.parse(String(startAt || ''));
  const e = Date.parse(String(endAt || ''));
  if (!Number.isFinite(s) || !Number.isFinite(e) || e < s) return null;
  return e - s;
}

function fmtMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return '-';
  return Math.round(n).toLocaleString('en-US');
}

function judgeCostCnyFromTokens(promptTokens, completionTokens) {
  const p = Number(promptTokens || 0);
  const c = Number(completionTokens || 0);
  if (!Number.isFinite(p) || !Number.isFinite(c)) return 0;
  const fen = (p / 1000) * JUDGE_INPUT_FEN_PER_KTOK + (c / 1000) * JUDGE_OUTPUT_FEN_PER_KTOK;
  return fen / 100;
}

function fmtCny(v, digits = 6) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return '0.000000';
  return n.toFixed(digits);
}

export default function JudgeTaskDetailPage() {
  const navigate = useNavigate();
  const { taskId } = useParams();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [task, setTask] = useState({});
  const [runDetail, setRunDetail] = useState({});
  const detailLoadingRef = useRef(false);

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadDetail = async ({ silent = false } = {}) => {
    if (!tenantId || !taskId) return;
    if (detailLoadingRef.current) return;
    detailLoadingRef.current = true;
    if (!silent) setLoading(true);
    try {
      const res = await getJudgeTask(tenantId, taskId);
      const t = res?.task || {};
      setTask(t);
      const rid = String(t?.run_id || '').trim();
      if (rid) {
        const detail = await getQaRunDetail(tenantId, rid);
        setRunDetail(detail || {});
      } else if (Array.isArray(t?.questions) && t.questions.length > 0) {
        setRunDetail({ questions: t.questions });
      } else {
        setRunDetail({});
      }
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载 Judge 任务详情失败');
    } finally {
      detailLoadingRef.current = false;
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    loadDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, taskId]);

  useEffect(() => {
    if (!isActive(task?.status)) return undefined;
    const timer = window.setInterval(() => {
      loadDetail({ silent: true }).catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, taskId, task?.status]);

  const questions = useMemo(() => (Array.isArray(runDetail?.questions) ? runDetail.questions : []), [runDetail]);

  const rows = useMemo(() => {
    return questions
      .filter((q) => q?.saved === true)
      .map((q, idx) => {
        const oj = q?.offline_judge || {};
        const decision = String(oj?.decision || '').toUpperCase();
        const overallScore = toTenPointScore(oj?.overall_score);
        const baselineScore = toTenPointScore(getJudgeBaselineScore(oj));
        const qualityScore = toTenPointScore(oj?.quality_score);
        const reasons = Array.isArray(oj?.reasons) ? oj.reasons : [];
        const stem = extractStem(q);
        const options = extractOptions(q);
        const answer = extractAnswer(q);
        const explanation = extractExplanation(q);
        const textbookSlice = extractTextbookSlice(q);
        const qualityConclusion = extractQualityConclusion(oj);
        const baselineConclusion = extractBaselineConclusion(oj);
        const promptTokens = Number(oj?.observability?.tokens?.prompt_tokens || 0);
        const completionTokens = Number(oj?.observability?.tokens?.completion_tokens || 0);
        const judgeLatencyMs = Number(oj?.observability?.latency_ms || 0);
        const judgeTokens = Number(oj?.observability?.tokens?.total_tokens || 0);
        const judgeCostCny = judgeCostCnyFromTokens(promptTokens, completionTokens);
        return {
          key: String(q?.question_id || idx + 1),
          index: idx + 1,
          question_id: String(q?.question_id || ''),
          stem,
          options,
          answer,
          explanation,
          textbookSlice,
          decision,
          overallScore: overallScore == null ? '-' : Number(overallScore).toFixed(2),
          baselineScore: baselineScore == null ? '-' : Number(baselineScore).toFixed(2),
          qualityScore: qualityScore == null ? '-' : Number(qualityScore).toFixed(2),
          reasons,
          reasonsText: reasons.join('；'),
          baselineConclusion,
          qualityConclusion,
          judgeLatencyMs,
          judgeTokens,
          judgeCostCny,
          oj,
        };
      });
  }, [questions]);

  const columns = [
    { title: '题号', dataIndex: 'index', width: 70 },
    {
      title: 'question_id',
      dataIndex: 'question_id',
      width: 260,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={v || '-'}>
          <span>{v || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '题目信息',
      dataIndex: 'stem',
      width: 560,
      render: (_, r) => {
        const optionsText = (r.options || []).map((opt, i) => `${String.fromCharCode(65 + i)}. ${opt}`).join('\n');
        const block = [
          `题干：${r.stem || '-'}`,
          `选项：\n${optionsText || '-'}`,
          `解析：${r.explanation || '-'}`,
          `切片原文：${r.textbookSlice || '(无切片原文)'}`,
        ].join('\n\n');
        return (
          <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} copyable>
            {block}
          </Paragraph>
        );
      },
    },
    {
      title: 'Judge',
      dataIndex: 'decision',
      width: 120,
      render: (v) => {
        const s = String(v || '');
        const color = s === 'PASS' ? 'green' : s === 'REVIEW' ? 'orange' : s === 'REJECT' ? 'red' : 'default';
        return <Tag color={color}>{s || '-'}</Tag>;
      },
    },
    { title: '融合总分', dataIndex: 'overallScore', width: 100 },
    { title: '基线分', dataIndex: 'baselineScore', width: 90 },
    {
      title: '基线分结论',
      dataIndex: 'baselineConclusion',
      width: 260,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{v || '-'}</span>}>
          <span>{v || '-'}</span>
        </Tooltip>
      ),
    },
    { title: '质量分', dataIndex: 'qualityScore', width: 90 },
    { title: 'Judge耗时(ms)', dataIndex: 'judgeLatencyMs', width: 120, render: (v) => fmtMs(v) },
    { title: 'Judge Tokens', dataIndex: 'judgeTokens', width: 110, render: (v) => fmtMs(v) },
    { title: 'Judge成本(元)', dataIndex: 'judgeCostCny', width: 130, render: (v) => fmtCny(v) },
    {
      title: '质量分结论(独立模型)',
      dataIndex: 'qualityConclusion',
      width: 260,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{v || '-'}</span>}>
          <span>{v || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '原因',
      dataIndex: 'reasonsText',
      width: 420,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={<span style={{ whiteSpace: 'pre-wrap' }}>{v || '-'}</span>}>
          <span>{v || '-'}</span>
        </Tooltip>
      ),
    },
  ];

  const cur = Number(task?.progress?.current || 0);
  const total = Number(task?.progress?.total || 0);
  const bm = (runDetail?.batch_metrics && typeof runDetail.batch_metrics === 'object') ? runDetail.batch_metrics : {};
  const taskElapsedMs = diffMs(task?.created_at, task?.updated_at);
  const judgeTotalCostCny = judgeCostCnyFromTokens(bm.judge_total_prompt_tokens, bm.judge_total_completion_tokens);
  const judgeAvgCostCny = Number(rows.length || 0) > 0 ? judgeTotalCostCny / Number(rows.length || 0) : 0;

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card>
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space>
            <Button type="primary" onClick={() => navigate('/judge-tasks')}>返回任务列表</Button>
            <Button onClick={() => loadDetail()} loading={loading}>刷新</Button>
            {isActive(task?.status) ? (
              <Button
                danger
                loading={cancelling}
                onClick={async () => {
                  if (!tenantId || !taskId) return;
                  setCancelling(true);
                  try {
                    await cancelJudgeTask(tenantId, taskId);
                    message.success('已请求取消');
                    await loadDetail({ silent: true });
                  } catch (e) {
                    message.error(e?.response?.data?.error?.message || '取消失败');
                  } finally {
                    setCancelling(false);
                  }
                }}
              >
                取消任务
              </Button>
            ) : null}
          </Space>
          <Typography.Text type="secondary">
            run: {String(task?.run_id || '-') || '-'}
          </Typography.Text>
        </Space>
      </Card>

      <Card>
        <Descriptions column={2} size="small">
          <Descriptions.Item label="任务名称">{String(task?.task_name || '-') || '-'}</Descriptions.Item>
          <Descriptions.Item label="状态">{String(task?.status || '-') || '-'}</Descriptions.Item>
          <Descriptions.Item label="task_id">{String(task?.task_id || '-') || '-'}</Descriptions.Item>
          <Descriptions.Item label="进度">{`${cur}/${total}`}</Descriptions.Item>
          <Descriptions.Item label="创建时间">{String(task?.created_at || '-') || '-'}</Descriptions.Item>
          <Descriptions.Item label="更新时间">{String(task?.updated_at || '-') || '-'}</Descriptions.Item>
          <Descriptions.Item label="任务耗时(ms)">{taskElapsedMs != null ? fmtMs(taskElapsedMs) : '-'}</Descriptions.Item>
          <Descriptions.Item label="Judge总耗时(ms)">{fmtMs(bm.judge_total_latency_ms)}</Descriptions.Item>
          <Descriptions.Item label="Judge平均耗时(ms/题)">{Number(bm.judge_avg_latency_ms_per_question || 0).toFixed(2)}</Descriptions.Item>
          <Descriptions.Item label="Judge总成本(元)">{fmtCny(judgeTotalCostCny)}</Descriptions.Item>
          <Descriptions.Item label="Judge平均成本(元/题)">{fmtCny(judgeAvgCostCny)}</Descriptions.Item>
          <Descriptions.Item label="Judge总Tokens">{fmtMs(bm.judge_total_tokens)}</Descriptions.Item>
          <Descriptions.Item label="Judge总调用数">{fmtMs(bm.judge_total_llm_calls)}</Descriptions.Item>
          <Descriptions.Item label="错误" span={2}>{(task?.errors || []).join(' ; ') || '-'}</Descriptions.Item>
          <Descriptions.Item label="运行详情" span={2}>
            {task?.run_id ? <Link to={`/qa-evaluation`}>前往质量评估查看 run</Link> : '-'}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title={`落库题 Judge 结果（${rows.length}）`}>
        <Table
          rowKey="key"
          columns={columns}
          dataSource={rows}
          size="small"
          loading={loading}
          expandable={{
            expandedRowRender: (r) => (
              <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label="question_id">{r.question_id || '-'}</Descriptions.Item>
                <Descriptions.Item label="题干">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} copyable>{r.stem || '-'}</Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="选项">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                    {(r.options || []).map((opt, i) => `${String.fromCharCode(65 + i)}. ${opt}`).join('\n') || '-'}
                  </Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="正确答案">{r.answer || '-'}</Descriptions.Item>
                <Descriptions.Item label="解析">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} copyable>{r.explanation || '-'}</Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="切片原文">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} copyable>{r.textbookSlice || '(无切片原文)'}</Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="Judge结论">
                  <Tag color={r.decision === 'PASS' ? 'green' : r.decision === 'REJECT' ? 'red' : r.decision === 'REVIEW' ? 'orange' : 'default'}>
                    {r.decision || '-'}
                  </Tag>
                  <Text> 融合总分 {r.overallScore} / 基线分 {r.baselineScore} / 质量分 {r.qualityScore}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="Judge资源消耗">
                  <Text>
                    耗时 {fmtMs(r.judgeLatencyMs)} ms / Tokens {fmtMs(r.judgeTokens)} / 成本 {fmtCny(r.judgeCostCny)} 元
                  </Text>
                </Descriptions.Item>
                <Descriptions.Item label="最新质量分结论">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                    {r.qualityConclusion || '该题未落盘独立质量分结论（quality_scoring_basis/quality_reasons）；请重跑最新 Judge 后查看'}
                  </Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="最新基线分结论">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                    {r.baselineConclusion || '该题未生成可读基线结论（dimension_results/reasons 为空）'}
                  </Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="Judge原因">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                    {(r.reasons || []).join('\n') || '-'}
                  </Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="Judge可执行建议">
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                    {r.oj?.actionable_feedback || '-'}
                  </Paragraph>
                </Descriptions.Item>
              </Descriptions>
            ),
          }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: 2000 }}
        />
      </Card>
    </Space>
  );
}
