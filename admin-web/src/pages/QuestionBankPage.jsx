import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Tag, message, Modal, Popconfirm, Select, Space, Table } from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  deleteBankQuestions,
  exportBankQuestions,
  listBankQuestions,
  listMaterials,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import QuestionDetailView from '../components/QuestionDetailView';

export default function QuestionBankPage() {
  const navigate = useNavigate();
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

  const fmtScore = (raw) => {
    if (raw === null || raw === undefined || raw === '') return '-';
    const n = Number(raw);
    if (!Number.isFinite(n)) return String(raw);
    const score = n > 10 ? n / 10 : n;
    return score.toFixed(2);
  };

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
        <Button
          size="small"
          onClick={() => navigate(`/question-bank/${encodeURIComponent(String(record?.question_id || ''))}/tune`, { state: { question: record } })}
        >
          调优
        </Button>
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
    <div style={{ width: '100%', minWidth: 0 }}>
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
    </div>
  );
}
