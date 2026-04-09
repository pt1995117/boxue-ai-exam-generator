import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Tag, Typography, message, Modal, Popconfirm, Select, Space, Table } from 'antd';
import {
  deleteBankQuestions,
  exportBankQuestions,
  listBankQuestions,
  listMaterials,
  optimizeBankQuestion,
  updateBankQuestion,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import QuestionDetailView from '../components/QuestionDetailView';

export default function QuestionBankPage() {
  const { TextArea } = Input;
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [keyword, setKeyword] = useState('');
  const [templateRole, setTemplateRole] = useState('all');
  const [templateRoutePrefix, setTemplateRoutePrefix] = useState('');
  const [templateMastery, setTemplateMastery] = useState('');
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('__all__');
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [rows, setRows] = useState([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [viewQuestionOpen, setViewQuestionOpen] = useState(false);
  const [viewQuestionRecord, setViewQuestionRecord] = useState(null);
  const [tuneOpen, setTuneOpen] = useState(false);
  const [tuneRecord, setTuneRecord] = useState(null);
  const [tuneFeedback, setTuneFeedback] = useState('');
  const [tuneJsonText, setTuneJsonText] = useState('');
  const [tuneOptimizing, setTuneOptimizing] = useState(false);
  const [tuneSaving, setTuneSaving] = useState(false);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 50, total: 0 });
  const materialLabel = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    const name = raw.replace(/^v\d{8}_\d{6}_/, '') || raw || m?.material_version_id;
    return `${name}${m?.status === 'effective' ? '（生效）' : ''}`;
  };

  const loadData = async (page = pagination.current, pageSize = pagination.pageSize) => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const params = { page, page_size: pageSize, keyword };
      if (templateRole === 'formal') params.template_official = 1;
      if (templateRole === 'backup') params.template_backup = 1;
      if (templateRoutePrefix.trim()) params.template_route_prefix = templateRoutePrefix.trim();
      if (templateMastery.trim()) params.template_mastery = templateMastery.trim();
      if (materialVersionId) params.material_version_id = materialVersionId;
      const res = await listBankQuestions(tenantId, params);
      setRows(res.items || []);
      setPagination({
        current: res.page || page,
        pageSize: res.page_size || pageSize,
        total: res.total || 0,
      });
    } catch (e) {
      const apiMsg = e?.response?.data?.error?.message;
      const status = e?.response?.status;
      if (status === 404) {
        message.error('加载题库失败：后端接口不存在，请重启 admin_api.py 到最新版本');
      } else {
        message.error(apiMsg || `加载题库失败（HTTP ${status || 'unknown'}）`);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    setSelectedRowKeys([]);
    loadData(1, pagination.pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, materialVersionId]);

  useEffect(() => {
    if (!tenantId) return;
    listMaterials(tenantId)
      .then((res) => setMaterials(res.items || []))
      .catch(() => setMaterials([]));
  }, [tenantId]);

  const onBatchDelete = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选题目');
      return;
    }
    try {
      const res = await deleteBankQuestions(tenantId, { question_ids: selectedRowKeys });
      message.success(`已删除 ${res.deleted || 0} 题`);
      loadData(pagination.current, pagination.pageSize);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '删除失败');
    }
  };

  const onBatchExport = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选题目');
      return;
    }
    setExporting(true);
    try {
      const blob = await exportBankQuestions(tenantId, {
        question_ids: selectedRowKeys,
        only_template_official: true,
      });
      const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
      const filename = `${tenantId}_题库导出_${ts}.xlsx`;
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      window.URL.revokeObjectURL(url);
      message.success(`导出成功（按模板正式题过滤，已选${selectedRowKeys.length}题）`);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '导出失败');
    } finally {
      setExporting(false);
    }
  };

  const onSearch = () => {
    setSelectedRowKeys([]);
    loadData(1, pagination.pageSize);
  };

  const openTuneModal = (record) => {
    setTuneRecord(record);
    setTuneFeedback('');
    setTuneJsonText(JSON.stringify(record || {}, null, 2));
    setTuneOpen(true);
  };

  const parseTuneJson = () => {
    try {
      const parsed = JSON.parse(tuneJsonText || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        message.error('题目JSON必须是对象');
        return null;
      }
      return parsed;
    } catch (_) {
      message.error('题目JSON格式不合法，请修正后再操作');
      return null;
    }
  };

  const onAiOptimize = async () => {
    if (!tuneRecord) return;
    const feedback = String(tuneFeedback || '').trim();
    if (!feedback) {
      message.warning('请先输入反馈意见');
      return;
    }
    const draft = parseTuneJson();
    if (!draft) return;
    setTuneOptimizing(true);
    try {
      const res = await optimizeBankQuestion(tenantId, {
        question_id: tuneRecord.question_id,
        feedback,
        question: draft,
      });
      const nextItem = res?.item || {};
      setTuneJsonText(JSON.stringify(nextItem, null, 2));
      message.success('AI调优完成，请确认并按需手工修改后保存');
    } catch (e) {
      message.error(e?.response?.data?.error?.message || 'AI调优失败');
    } finally {
      setTuneOptimizing(false);
    }
  };

  const onSaveTune = async () => {
    if (!tuneRecord) return;
    const item = parseTuneJson();
    if (!item) return;
    if (!String(item?.题干 || '').trim()) {
      message.warning('题干不能为空');
      return;
    }
    setTuneSaving(true);
    try {
      await updateBankQuestion(tenantId, {
        question_id: tuneRecord.question_id,
        item,
      });
      message.success('题目已按最终确认版本保存到题库');
      setTuneOpen(false);
      setTuneRecord(null);
      setTuneFeedback('');
      setTuneJsonText('');
      await loadData(pagination.current, pagination.pageSize);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存失败');
    } finally {
      setTuneSaving(false);
    }
  };

  const fmtScore = (raw) => {
    if (raw === null || raw === undefined || raw === '') return '-';
    const n = Number(raw);
    if (!Number.isFinite(n)) return String(raw);
    const score = n > 10 ? n / 10 : n;
    return score.toFixed(2);
  };

  const PAGE_WIDTH = 1680;
  const TABLE_SCROLL_X = 2800;

  const columns = [
    { title: 'ID', dataIndex: 'question_id', width: 90 },
    { title: '题干', dataIndex: '题干', ellipsis: true, width: 320 },
    { title: '答案', dataIndex: '正确答案', width: 100 },
    {
      title: '出题任务',
      dataIndex: 'source_task_name',
      width: 220,
      ellipsis: true,
      render: (_, record) => record?.source_task_name || record?.出题任务名称 || record?.source_task_id || record?.出题任务ID || '-',
    },
    {
      title: '模板身份',
      dataIndex: '模板正式题',
      width: 120,
      render: (_, record) => {
        const isFormal = Boolean(record?.模板正式题);
        const isBackup = Boolean(record?.模板备选题);
        if (isFormal) return <Tag color="green">正式题</Tag>;
        if (isBackup) return <Tag color="orange">备选题</Tag>;
        return <Tag>普通题</Tag>;
      },
    },
    { title: '模板位次', dataIndex: '模板目标位次', width: 100, render: (v) => v || '-' },
    {
      title: '模板路由前缀',
      dataIndex: '模板路由前缀',
      width: 220,
      ellipsis: true,
      render: (_, record) => record?.模板路由前缀 || record?.模板路由 || '-',
    },
    {
      title: '模板掌握程度',
      dataIndex: '模板掌握程度',
      width: 120,
      render: (_, record) => record?.模板掌握程度 || record?.模板掌握度 || '-',
    },
    {
      title: '最终分',
      dataIndex: 'offline_judge_score',
      width: 100,
      render: (_, record) => fmtScore(record?.offline_judge_score ?? record?.离线Judge评分),
    },
    {
      title: 'Judge结论',
      dataIndex: 'offline_judge_decision',
      width: 110,
      render: (_, record) => {
        const d = String(record?.offline_judge_decision || record?.离线Judge结论 || '').trim().toLowerCase();
        if (!d) return '-';
        const color = d === 'pass' ? 'green' : d === 'review' ? 'orange' : d === 'reject' ? 'red' : 'default';
        return <Tag color={color}>{d}</Tag>;
      },
    },
    {
      title: '质量分',
      dataIndex: 'offline_judge_quality_score',
      width: 100,
      render: (_, record) => fmtScore(record?.offline_judge_quality_score),
    },
    {
      title: '基准分',
      dataIndex: 'offline_judge_baseline_score',
      width: 100,
      render: (_, record) => fmtScore(record?.offline_judge_baseline_score),
    },
    {
      title: '质量分结论',
      dataIndex: 'offline_judge_quality_conclusion',
      width: 260,
      ellipsis: true,
      render: (v) => (
        <span title={String(v || '-')}>{String(v || '-')}</span>
      ),
    },
    {
      title: '基准分结论',
      dataIndex: 'offline_judge_baseline_conclusion',
      width: 260,
      ellipsis: true,
      render: (v) => (
        <span title={String(v || '-')}>{String(v || '-')}</span>
      ),
    },
    { title: '教材版本', dataIndex: '教材版本ID', width: 170, render: (v) => v || 'legacy' },
    { title: '来源路径', dataIndex: '来源路径', ellipsis: true, width: 320 },
    {
      title: '调优',
      dataIndex: '_tune',
      width: 90,
      fixed: 'right',
      render: (_, record) => (
        <Button size="small" onClick={() => openTuneModal(record)}>调优</Button>
      ),
    },
    {
      title: '查看',
      dataIndex: '_view',
      width: 90,
      fixed: 'right',
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
  ];

  return (
    <div style={{ width: PAGE_WIDTH, maxWidth: PAGE_WIDTH }}>
      <Space className="toolbar" wrap>
        <Select
          value={materialVersionId}
          style={{ width: 240 }}
          onChange={setMaterialVersionId}
          options={[
            { label: '全部教材', value: '__all__' },
            ...materials.map((m) => ({
              label: materialLabel(m),
              value: m.material_version_id,
            })),
          ]}
        />
        <Input
          placeholder="题干关键词"
          style={{ width: 260 }}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <Select
          value={templateRole}
          style={{ width: 140 }}
          onChange={setTemplateRole}
          options={[
            { label: '全部身份', value: 'all' },
            { label: '仅正式题', value: 'formal' },
            { label: '仅备选题', value: 'backup' },
          ]}
        />
        <Input
          placeholder="模板路由前缀"
          style={{ width: 220 }}
          value={templateRoutePrefix}
          onChange={(e) => setTemplateRoutePrefix(e.target.value)}
        />
        <Select
          value={templateMastery}
          style={{ width: 140 }}
          onChange={setTemplateMastery}
          options={[
            { label: '全部掌握程度', value: '' },
            { label: '了解', value: '了解' },
            { label: '熟悉', value: '熟悉' },
            { label: '掌握', value: '掌握' },
          ]}
        />
        <Button type="primary" onClick={onSearch}>查询</Button>
      </Space>

      <Card style={{ marginBottom: 12 }}>
        <Alert type="info" showIcon message={`已选 ${selectedRowKeys.length} 条，可批量导出或删除`} />
        <Space style={{ marginTop: 12 }}>
          <Button onClick={onBatchExport} loading={exporting} disabled={!selectedRowKeys.length}>批量导出</Button>
          <Popconfirm title="确认删除选中题目？" onConfirm={onBatchDelete}>
            <Button danger disabled={!selectedRowKeys.length}>批量删除</Button>
          </Popconfirm>
        </Space>
      </Card>

      <Card>
        <Table
          rowKey="question_id"
          loading={loading}
          columns={columns}
          dataSource={rows}
          tableLayout="fixed"
          scroll={{ x: TABLE_SCROLL_X }}
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys, preserveSelectedRowKeys: true }}
          pagination={{
            current: pagination.current,
            pageSize: pagination.pageSize,
            total: pagination.total,
            showSizeChanger: true,
          }}
          onChange={(pg) => loadData(pg.current, pg.pageSize)}
        />
      </Card>
      <Modal
        title="题目详情"
        open={viewQuestionOpen}
        onCancel={() => {
          setViewQuestionOpen(false);
          setViewQuestionRecord(null);
        }}
        footer={null}
        width={900}
        styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
      >
        <QuestionDetailView question={viewQuestionRecord || {}} />
      </Modal>

      <Modal
        title="单题调优"
        open={tuneOpen}
        width={980}
        onCancel={() => {
          if (tuneOptimizing || tuneSaving) return;
          setTuneOpen(false);
          setTuneRecord(null);
          setTuneFeedback('');
          setTuneJsonText('');
        }}
        onOk={onSaveTune}
        okText="保存最终版本"
        confirmLoading={tuneSaving}
        maskClosable={false}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          <Alert
            type="info"
            showIcon
            message="先填写反馈并触发 AI 重写；也可直接手改下方 JSON。保存后会覆盖该题在题库中的最终版本。"
          />
          <Typography.Text strong>反馈意见</Typography.Text>
          <TextArea
            value={tuneFeedback}
            onChange={(e) => setTuneFeedback(e.target.value)}
            rows={3}
            placeholder="示例：题干太长，请改成情境化且更聚焦考点；错误选项要更有迷惑性。"
          />
          <Space>
            <Button loading={tuneOptimizing} onClick={onAiOptimize}>AI按反馈重新输出</Button>
            <Button
              onClick={() => setTuneJsonText(JSON.stringify(tuneRecord || {}, null, 2))}
              disabled={!tuneRecord || tuneOptimizing || tuneSaving}
            >
              重置为原题
            </Button>
          </Space>
          <Typography.Text strong>最终题目 JSON（可编辑任意字段）</Typography.Text>
          <TextArea
            value={tuneJsonText}
            onChange={(e) => setTuneJsonText(e.target.value)}
            rows={18}
            style={{ fontFamily: 'Menlo, Monaco, Consolas, monospace' }}
            placeholder="在此编辑最终题目 JSON"
          />
        </Space>
      </Modal>
    </div>
  );
}
